import os
import socket
import sys
import threading

TARGET_HOST = os.environ["TARGET_HOST"]
TARGET_PORT = int(os.environ.get("TARGET_PORT", "9000"))
NUM_CONNECTIONS = int(os.environ.get("NUM_CONNECTIONS", "10"))

SEND_RECV = [
    (b"HELLO SERVER \xf0\x9f\x91\x8b\n", b"HELLO CLIENT"),
    (b"HOW ARE YOU? \xf0\x9f\x94\x84\n", b"DOING GREAT"),
]


def run_connection(barrier: threading.Barrier, errors: list, idx: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((TARGET_HOST, TARGET_PORT))

        buf = b""
        for send_msg, expect_prefix in SEND_RECV:
            sock.sendall(send_msg)
            while expect_prefix not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    errors.append(f"conn {idx}: server closed early")
                    return
                buf += chunk
            idx_nl = buf.index(b"\n", buf.index(expect_prefix))
            buf = buf[idx_nl + 1:]

        # wait until all connections are ready to close simultaneously
        barrier.wait()

        sock.sendall(b"CLOSE\n")
        sock.shutdown(socket.SHUT_WR)
        while sock.recv(4096):
            pass
    except Exception as e:
        errors.append(f"conn {idx}: {e}")
    finally:
        sock.close()


def main() -> None:
    barrier = threading.Barrier(NUM_CONNECTIONS)
    errors: list = []
    threads = [
        threading.Thread(target=run_connection, args=(barrier, errors, i), daemon=True)
        for i in range(NUM_CONNECTIONS)
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
