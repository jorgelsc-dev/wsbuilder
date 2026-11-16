"""Rough HTTP/1.1 keep-alive throughput benchmark for wsbuilder.

Starts a server in a child process and drives it from separate client
processes, so the clients do not compete with the server for its GIL::

    python benchmarks/http1_keepalive.py --requests 4000 --concurrency 1 4 8

The numbers depend on the machine; compare runs on the same host only.
"""

import argparse
import multiprocessing
import os
import socket
import sys
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")


def serve(port, ready):
    sys.path.insert(0, ROOT)
    from wsbuilder import App, Response
    from wsbuilder.server import HTTPServer

    app = App()

    @app.api("/bench")
    def bench(_request):
        return Response.json({"ok": True})

    server = HTTPServer("127.0.0.1", port, app, max_keepalive_requests=0)
    server.HANDLE_SIGTERM = False
    ready.set()
    server.serve_forever()


def client(args):
    port, count = args
    request = b"GET /bench HTTP/1.1\r\nHost: bench\r\n\r\n"
    with socket.create_connection(("127.0.0.1", port)) as sock:
        for _ in range(count):
            sock.sendall(request)
            buffer = b""
            while not buffer.endswith(b'{"ok":true}'):
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("server closed the connection")
                buffer += chunk
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=18777)
    parser.add_argument("--requests", type=int, default=4000)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8])
    options = parser.parse_args()

    ready = multiprocessing.Event()
    server = multiprocessing.Process(target=serve, args=(options.port, ready), daemon=True)
    server.start()
    ready.wait(10)
    time.sleep(0.3)
    try:
        for concurrency in options.concurrency:
            per_client = max(1, options.requests // concurrency)
            started = time.perf_counter()
            with multiprocessing.Pool(concurrency) as pool:
                total = sum(pool.map(client, [(options.port, per_client)] * concurrency))
            elapsed = time.perf_counter() - started
            print(f"{concurrency:3d} connections: {total / elapsed:8.0f} req/s")
    finally:
        server.terminate()
        server.join(5)


if __name__ == "__main__":
    main()
