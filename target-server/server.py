import os
import socket
import sqlite3
import struct
import threading
import time

DB_PATH = os.environ.get("DB_PATH", "/data/cilium_repro.db")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9000"))
SS_POLL_INTERVAL = 2.0

_db_lock = threading.Lock()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS connections (
            id           INTEGER PRIMARY KEY,
            src_ip       TEXT,
            src_port     INTEGER,
            accepted_at  REAL,
            time_wait_at REAL,
            released_at  REAL,
            is_active    INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS ss_snapshots (
            id            INTEGER PRIMARY KEY,
            snapshot_time REAL,
            raw_ss_output TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_conn_src ON connections(src_ip, src_port);
    """)
    conn.commit()


def db_accept(db: sqlite3.Connection, src_ip: str, src_port: int) -> int:
    with _db_lock:
        cur = db.execute(
            "INSERT INTO connections (src_ip, src_port, accepted_at) VALUES (?, ?, ?)",
            (src_ip, src_port, time.time()),
        )
        db.commit()
        return cur.lastrowid


def db_close(db: sqlite3.Connection, conn_id: int) -> None:
    """Mark a connection inactive when handle() exits, even without TIME_WAIT."""
    with _db_lock:
        db.execute(
            "UPDATE connections SET is_active = 0 WHERE id = ? AND is_active = 1",
            (conn_id,),
        )
        db.commit()


def db_snapshot(db: sqlite3.Connection, output: str) -> None:
    with _db_lock:
        db.execute(
            "INSERT INTO ss_snapshots (snapshot_time, raw_ss_output) VALUES (?, ?)",
            (time.time(), output),
        )
        db.commit()


def db_set_time_wait(db: sqlite3.Connection, src_ip: str, src_port: int) -> None:
    with _db_lock:
        # Only update the most recent row for this src — ordered by id DESC so
        # we get the current connection, not a stale one from a previous use of
        # this SNAT port.
        db.execute(
            """UPDATE connections SET time_wait_at = ?
               WHERE id = (
                   SELECT id FROM connections
                   WHERE src_ip = ? AND src_port = ? AND time_wait_at IS NULL
                   ORDER BY id DESC LIMIT 1
               )""",
            (time.time(), src_ip, src_port),
        )
        db.commit()


def db_release(db: sqlite3.Connection, src_ip: str, src_port: int) -> None:
    with _db_lock:
        db.execute(
            """UPDATE connections SET released_at = ?, is_active = 0
               WHERE id = (
                   SELECT id FROM connections
                   WHERE src_ip = ? AND src_port = ? AND released_at IS NULL
                     AND time_wait_at IS NOT NULL
                   ORDER BY id DESC LIMIT 1
               )""",
            (time.time(), src_ip, src_port),
        )
        db.commit()


# --- /proc/net/tcp poller ----------------------------------------------------

_TCP_STATE_TIME_WAIT = 6  # 0x06


def _parse_proc_net_tcp() -> list[tuple[str, int, int]]:
    """Return list of (remote_ip, remote_port, state) for all TCP sockets."""
    entries = []
    try:
        with open("/proc/net/tcp") as f:
            next(f)  # skip header
            for line in f:
                parts = line.split()
                if len(parts) < 4:
                    continue
                rem_hex = parts[2]   # remote address: AABBCCDD:PPPP
                state = int(parts[3], 16)
                rem_addr, rem_port_hex = rem_hex.split(":")
                # kernel stores addresses little-endian
                ip = socket.inet_ntoa(struct.pack("<I", int(rem_addr, 16)))
                port = int(rem_port_hex, 16)
                entries.append((ip, port, state))
    except OSError:
        pass
    return entries


def ss_poller(db: sqlite3.Connection) -> None:
    """Background thread: poll /proc/net/tcp every SS_POLL_INTERVAL seconds."""
    prev_timewait: set[tuple[str, int]] = set()

    while True:
        try:
            entries = _parse_proc_net_tcp()
            snapshot_lines = [f"{ip}:{port} state={st}" for ip, port, st in entries]
            db_snapshot(db, "\n".join(snapshot_lines))

            current_timewait = {
                (ip, port)
                for ip, port, state in entries
                if state == _TCP_STATE_TIME_WAIT
            }

            for ip, port in current_timewait - prev_timewait:
                db_set_time_wait(db, ip, port)
            for ip, port in prev_timewait - current_timewait:
                db_release(db, ip, port)

            prev_timewait = current_timewait
        except Exception as e:
            print(f"[proc-poller] error: {e}", flush=True)

        time.sleep(SS_POLL_INTERVAL)


# --- connection handler -------------------------------------------------------

MESSAGES_TO_CLIENT = ["HELLO CLIENT 🎉\n", "DOING GREAT! ✅\n"]


def handle(conn: socket.socket, addr: tuple, db: sqlite3.Connection) -> None:
    src_ip, src_port = addr[0], addr[1]
    print(f"[server] ACCEPT {src_ip}:{src_port}", flush=True)
    conn_id = db_accept(db, src_ip, src_port)

    reply_idx = 0
    try:
        buf = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                msg = line.decode(errors="replace").strip()
                print(f"[server] RECV {src_ip}:{src_port} → {msg!r}", flush=True)

                if msg == "CLOSE":
                    # simultaneous FIN: send our FIN immediately
                    print(f"[server] CLOSE {src_ip}:{src_port}", flush=True)
                    conn.shutdown(socket.SHUT_WR)
                    # drain remaining bytes from client FIN
                    while conn.recv(4096):
                        pass
                    return

                if reply_idx < len(MESSAGES_TO_CLIENT):
                    conn.sendall(MESSAGES_TO_CLIENT[reply_idx].encode())
                    reply_idx += 1
    except OSError as e:
        print(f"[server] {src_ip}:{src_port} socket error: {e}", flush=True)
    finally:
        conn.close()
        db_close(db, conn_id)


# --- main --------------------------------------------------------------------

def main() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    init_db(db)

    poller = threading.Thread(target=ss_poller, args=(db,), daemon=True)
    poller.start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", LISTEN_PORT))
    srv.listen(128)
    print(f"[server] listening on :{LISTEN_PORT}, db={DB_PATH}", flush=True)

    while True:
        conn, addr = srv.accept()
        t = threading.Thread(target=handle, args=(conn, addr, db), daemon=True)
        t.start()


if __name__ == "__main__":
    main()
