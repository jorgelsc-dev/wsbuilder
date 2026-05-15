import socket
import ssl
from concurrent.futures import ThreadPoolExecutor

from .http import Request, Response, parse_http_request, send_http_response
from .ws import handshake_websocket, is_ws_request, recv_exact


class HTTPServer:
    MAX_CONNECTION_WORKERS = 64
    MAX_REQUEST_HEADER_BYTES = 64 * 1024
    MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024

    def __init__(self, host, port, app, ssl_context=None):
        self.host = host
        self.port = port
        self.app = app
        self._sock = None
        self.ssl_context = ssl_context

    def serve_forever(self):
        for hook in self.app.startup_hooks:
            try:
                hook()
            except Exception as e:
                print(f"[startup] error: {e}")

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(128)
        self._sock = s
        scheme = "https" if self.ssl_context else "http"
        print(f"Server listening on {scheme}://{self.host}:{self.port}/")
        interrupted = False
        try:
            with ThreadPoolExecutor(
                max_workers=self.MAX_CONNECTION_WORKERS,
                thread_name_prefix="framework-http",
            ) as pool:
                while True:
                    conn, addr = s.accept()
                    pool.submit(self.handle_conn, conn, addr)
        except KeyboardInterrupt:
            interrupted = True
            print("\n[shutdown] interrupted by user (Ctrl+C). stopping server...")
        finally:
            s.close()
            if interrupted:
                print("[shutdown] server stopped.")

    def handle_conn(self, conn, addr):
        tls_meta = {
            "enabled": bool(self.ssl_context),
            "peer_cert": None,
            "cipher": None,
            "version": None,
        }
        if self.ssl_context:
            try:
                conn = self.ssl_context.wrap_socket(conn, server_side=True)
                tls_meta["peer_cert"] = conn.getpeercert()
                tls_meta["cipher"] = conn.cipher()
                tls_meta["version"] = conn.version()
            except ssl.SSLError as e:
                print(f"[tls] handshake error from {addr}: {e}")
                try:
                    conn.close()
                except Exception:
                    pass
                return
            except Exception as e:
                print(f"[tls] wrap error from {addr}: {e}")
                try:
                    conn.close()
                except Exception:
                    pass
                return

        with conn:
            try:
                req = parse_http_request(
                    conn,
                    max_header_bytes=self.MAX_REQUEST_HEADER_BYTES,
                )
            except ValueError as e:
                message = str(e).lower()
                status = 431 if "header" in message else 400
                send_http_response(conn, Response.text(str(e), status=status))
                return
            if not req:
                return

            headers = req["headers"]
            body = req["remainder"]

            if "content-length" in headers:
                try:
                    cl = int(headers["content-length"])
                except Exception:
                    cl = 0
                if cl < 0:
                    cl = 0
                if cl > self.MAX_REQUEST_BODY_BYTES:
                    send_http_response(
                        conn,
                        Response.text("Payload Too Large", status=413),
                    )
                    return
                if len(body) < cl:
                    need = cl - len(body)
                    if need > 0:
                        body += recv_exact(conn, need)
                if len(body) > self.MAX_REQUEST_BODY_BYTES:
                    send_http_response(
                        conn,
                        Response.text("Payload Too Large", status=413),
                    )
                    return

            raw_path = req["path"]
            path, _, query = raw_path.partition("?")

            request = Request(
                method=req["method"],
                path=path,
                query_string=query,
                headers=headers,
                body=body,
                client=addr,
                tls=tls_meta,
            )

            if is_ws_request(headers):
                handler = self.app.ws_routes.get(path)
                if not handler:
                    send_http_response(conn, Response.text("Not Found", status=404))
                    return
                ws = handshake_websocket(conn, addr, headers)
                if not ws:
                    return
                try:
                    handler(ws, request)
                except Exception as e:
                    print(f"[ws] error: {e}")
                return

            response = self.app.dispatch(request)
            send_http_response(conn, response)

