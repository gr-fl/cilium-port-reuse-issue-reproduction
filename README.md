# Cilium TIME_WAIT / SNAT Port Reuse — Reproduction

Reproduces the bug where `scrubIPsInConntrackTableLocked()` prematurely
deletes TIME_WAIT conntrack entries when a pod dies, freeing SNAT ports
before the remote server's TIME_WAIT expires. A new pod assigned the same
SNAT port within that window triggers a RST loop on the server.

See `REQUIREMENTS.md` for full architecture and rationale.

## Prerequisites

- A Kubernetes cluster running Cilium
- A node you can cordon to isolate client pods (`kubectl cordon <node>`)
- A host outside the cluster to run the target server (needs to be
  reachable from the cluster node via a routable IP, triggering SNAT)
- Docker on the target server host
- A container registry accessible from both your machine and the cluster

## Topology

```
[target server]          [k8s cluster]
  Docker container  <──  client pods (on cordoned node, via SNAT)
  records TIME_WAIT       orchestrator pod (creates client Jobs)
```

The client pods must connect to an IP external to the cluster node so
that Cilium applies SNAT. The target server must be outside the cluster.

## Images

| Component | Tag |
|---|---|
| Target server | `<your-registry>/cilium-repro-server:latest` |
| Client job | `<your-registry>/cilium-repro-client:latest` |
| Orchestrator | `<your-registry>/cilium-repro-orchestrator:latest` |

## Build and push

```bash
export REGISTRY=<your-registry>

cd target-server
docker build -t $REGISTRY/cilium-repro-server:latest .
docker push $REGISTRY/cilium-repro-server:latest

cd ../client
docker build -t $REGISTRY/cilium-repro-client:latest .
docker push $REGISTRY/cilium-repro-client:latest

cd ../orchestration
docker build -t $REGISTRY/cilium-repro-orchestrator:latest .
docker push $REGISTRY/cilium-repro-orchestrator:latest

cd ../ui
docker build -t $REGISTRY/cilium-repro-ui:latest .
docker push $REGISTRY/cilium-repro-ui:latest
```

## Running the target server

Run on a host outside the cluster. The SQLite DB is written to
`/data/cilium_repro.db` — bind-mount a local directory.

```bash
docker run -d \
  --name cilium-repro-server \
  -p 9000:9000 \
  -v /path/to/data:/data \
  -e DB_PATH=/data/cilium_repro.db \
  -e LISTEN_PORT=9000 \
  <your-registry>/cilium-repro-server:latest
```

## Running the UI

```bash
docker run -d \
  --name cilium-repro-ui \
  -p 8001:8001 \
  -v /path/to/data:/data \
  -e DB_PATH=/data/cilium_repro.db \
  <your-registry>/cilium-repro-ui:latest
```

Open `http://<target-server-host>:8001`. The dashboard auto-refreshes
every 5 seconds and highlights port reuse events with gap < 60s in red.

## Deploying to Kubernetes

Edit `k8s/orchestration-deployment.yaml` to set:
- `<your-cordoned-node>` — the node to pin client pods to
- `<ip-or-hostname-of-target-server>` — reachable from the cluster node
- `<your-registry>` — your container registry

Then:

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/orchestration-deployment.yaml
```

## Monitoring

```bash
# Watch client jobs being created
kubectl -n cilium-repro get jobs -w

# Orchestrator logs
kubectl -n cilium-repro logs deployment/cilium-repro-orchestrator -f
```

## What to look for

In the UI, the **Port reuse events** section shows cases where a SNAT port
was reused while the server still had an active TIME_WAIT entry. Any event
with gap < 60s is a bug hit — Cilium freed the port before the kernel's
`tcp_fin_timeout` window elapsed.

You can also query the SQLite DB directly:

```bash
docker exec cilium-repro-server python3 -c "
import sqlite3
db = sqlite3.connect('/data/cilium_repro.db')
rows = db.execute('''
    SELECT a.src_port, b.accepted_at - a.time_wait_at AS gap_secs
    FROM connections a
    JOIN connections b ON a.src_port = b.src_port AND b.id > a.id
    WHERE a.time_wait_at IS NOT NULL
      AND b.accepted_at > a.time_wait_at
    ORDER BY gap_secs
''').fetchall()
print(f'{len(rows)} reuse events')
for port, gap in rows:
    flag = '  <-- BUG' if gap < 60 else ''
    print(f'  port={port}  gap={gap:.1f}s{flag}')
"
```

## Tuning

| Env var | Default | Effect |
|---|---|---|
| `TARGET_RATE_PER_MIN` | 20 | Pod creation rate (Poisson) |
| `NUM_CONNECTIONS` | 10 | Connections per pod (simulates connection pool) |

Increasing `NUM_CONNECTIONS` frees more SNAT ports simultaneously on each
pod death, increasing the probability of collision. Cordoning the node
reduces the ephemeral port pool available for SNAT, also increasing
collision probability.
