import os
import random
import time
import uuid

from kubernetes import client, config
from kubernetes.client import V1Job

TARGET_HOST = os.environ["TARGET_HOST"]
TARGET_PORT = os.environ.get("TARGET_PORT", "9000")
CLIENT_IMAGE = os.environ["CLIENT_IMAGE"]
NAMESPACE = os.environ.get("NAMESPACE", "cilium-repro")
NODE_NAME = os.environ["NODE_NAME"]  # cordoned node to pin client pods to
NUM_CONNECTIONS = os.environ.get("NUM_CONNECTIONS", "10")
RATE_PER_MIN = float(os.environ.get("TARGET_RATE_PER_MIN", "20"))
RATE_PER_SEC = RATE_PER_MIN / 60.0

JOB_TTL_SECONDS = 120  # k8s cleans up finished Jobs automatically


def build_job(job_name: str) -> V1Job:
    return V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(
            name=job_name,
            namespace=NAMESPACE,
            labels={"app": "cilium-repro-client"},
        ),
        spec=client.V1JobSpec(
            ttl_seconds_after_finished=JOB_TTL_SECONDS,
            backoff_limit=0,
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={"app": "cilium-repro-client"}
                ),
                spec=client.V1PodSpec(
                    restart_policy="Never",
                    node_selector={"kubernetes.io/hostname": NODE_NAME},
                    tolerations=[
                        client.V1Toleration(
                            key="node.kubernetes.io/unschedulable",
                            operator="Exists",
                            effect="NoSchedule",
                        )
                    ],
                    security_context=client.V1PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=65534,
                        seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[
                        client.V1Container(
                            name="client",
                            image=CLIENT_IMAGE,
                            security_context=client.V1SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=client.V1Capabilities(drop=["ALL"]),
                            ),
                            env=[
                                client.V1EnvVar(name="TARGET_HOST", value=TARGET_HOST),
                                client.V1EnvVar(name="TARGET_PORT", value=TARGET_PORT),
                                client.V1EnvVar(name="NUM_CONNECTIONS", value=NUM_CONNECTIONS),
                            ],
                        )
                    ],
                ),
            ),
        ),
    )


def main() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    batch = client.BatchV1Api()
    print(
        f"[orchestrator] starting: rate={RATE_PER_MIN}/min node={NODE_NAME} target={TARGET_HOST}:{TARGET_PORT}",
        flush=True,
    )

    while True:
        # Poisson inter-arrival: exponential with mean = 1/rate
        delay = random.expovariate(RATE_PER_SEC)
        time.sleep(delay)

        job_name = f"cilium-repro-client-{uuid.uuid4().hex[:8]}"
        job = build_job(job_name)
        try:
            batch.create_namespaced_job(namespace=NAMESPACE, body=job)
            print(f"[orchestrator] created job {job_name} (slept {delay:.2f}s)", flush=True)
        except Exception as e:
            print(f"[orchestrator] failed to create job {job_name}: {e}", flush=True)


if __name__ == "__main__":
    main()
