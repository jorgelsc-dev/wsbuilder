import socket
import ssl
import threading
import time

from .http import Request, Response, parse_http_request, send_http_response
from .ws import handshake_websocket, is_ws_request, recv_exact


class HTTPServer:
    MAX_CONNECTION_WORKERS = 64
    MAX_REQUEST_HEADER_BYTES = 64 * 1024
    MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
    ACCEPT_TIMEOUT_SECONDS = 0.5
    ACQUIRE_WORKER_TIMEOUT_SECONDS = 1.0
    REQUEST_READ_TIMEOUT_SECONDS = 10.0

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
        s.settimeout(self.ACCEPT_TIMEOUT_SECONDS)
        self._sock = s
        scheme = "https" if self.ssl_context else "http"
        print(f"Server listening on {scheme}://{self.host}:{self.port}/")
        worker_limiter = threading.BoundedSemaphore(self.MAX_CONNECTION_WORKERS)
        interrupted = False
        try:
            while True:
                try:
                    conn, addr = s.accept()
                except socket.timeout:
                    continue
                acquired = worker_limiter.acquire(timeout=self.ACQUIRE_WORKER_TIMEOUT_SECONDS)
                if not acquired:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
                t = threading.Thread(
                    target=self._handle_conn_with_release,
                    args=(conn, addr, worker_limiter),
                    name=f"framework-http-{addr[0]}:{addr[1]}",
                    daemon=True,
                )
                t.start()
        except KeyboardInterrupt:
            interrupted = True
            print("\n[shutdown] interrupted by user (Ctrl+C). stopping server...")
        finally:
            s.close()
            try:
                if hasattr(self.app, "close"):
                    self.app.close()
            except Exception as e:
                print(f"[shutdown] app.close() error: {e}")
            if interrupted:
                print("[shutdown] server stopped.")

    def _handle_conn_with_release(self, conn, addr, limiter):
        metrics = getattr(self.app, "metrics", None)
        if metrics:
            metrics.tcp_connection_open()
        try:
            self.handle_conn(conn, addr)
        finally:
            if metrics:
                metrics.tcp_connection_close()
            limiter.release()

    def handle_conn(self, conn, addr):
        metrics = getattr(self.app, "metrics", None)
        tls_meta = {
            "enabled": bool(self.ssl_context),
            "peer_cert": None,
            "cipher": None,
            "version": None,
        }
        if self.ssl_context:
            try:
                conn = self.ssl_context.wrap_socket(conn, server_side=True)
                conn.settimeout(self.REQUEST_READ_TIMEOUT_SECONDS)
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
                conn.settimeout(self.REQUEST_READ_TIMEOUT_SECONDS)
            except Exception:
                pass
            try:
                req = parse_http_request(
                    conn,
                    max_header_bytes=self.MAX_REQUEST_HEADER_BYTES,
                )
            except socket.timeout:
                send_http_response(conn, Response.text("Request Timeout", status=408))
                return
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
                        try:
                            body += recv_exact(conn, need)
                        except socket.timeout:
                            send_http_response(conn, Response.text("Request Timeout", status=408))
                            return
                if len(body) > self.MAX_REQUEST_BODY_BYTES:
                    send_http_response(
                        conn,
                        Response.text("Payload Too Large", status=413),
                    )
                    return

            raw_path = req["path"]
            path, _, query = raw_path.partition("?")
            started = time.time()

            request = Request(
                method=req["method"],
                path=path,
                query_string=query,
                headers=headers,
                body=body,
                client=addr,
                tls=tls_meta,
            )
            if metrics:
                metrics.http_request_started(
                    request.method,
                    request.path,
                    body_size=len(request.body),
                )

            if is_ws_request(headers):
                handler = self.app.ws_routes.get(path)
                if not handler:
                    response = Response.text("Not Found", status=404)
                    send_http_response(conn, response)
                    if metrics:
                        elapsed = (time.time() - started) * 1000.0
                        metrics.http_response_sent(
                            request.method,
                            request.path,
                            response.status,
                            body_size=len(response.body),
                            duration_ms=elapsed,
                        )
                    return
                ws = handshake_websocket(conn, addr, headers)
                if not ws:
                    if metrics:
                        elapsed = (time.time() - started) * 1000.0
                        metrics.error("ws_handshake", "failed")
                        metrics.http_response_sent(
                            request.method,
                            request.path,
                            400,
                            body_size=0,
                            duration_ms=elapsed,
                        )
                    return
                if metrics:
                    metrics.ws_opened(path)
                    elapsed = (time.time() - started) * 1000.0
                    metrics.http_response_sent(
                        request.method,
                        request.path,
                        101,
                        body_size=0,
                        duration_ms=elapsed,
                    )
                try:
                    handler(ws, request)
                except Exception as e:
                    print(f"[ws] error: {e}")
                    if metrics:
                        metrics.error("ws_handler", e)
                finally:
                    if metrics:
                        metrics.ws_closed(path)
                return

            try:
                response = self.app.dispatch(request)
            except Exception as e:
                if metrics:
                    elapsed = (time.time() - started) * 1000.0
                    metrics.error("http_dispatch", e)
                    metrics.http_response_sent(
                        request.method,
                        request.path,
                        500,
                        body_size=0,
                        duration_ms=elapsed,
                    )
                send_http_response(conn, Response.text("Internal Server Error", status=500))
                return

            send_http_response(conn, response)
            if metrics:
                elapsed = (time.time() - started) * 1000.0
                body_size = 0 if response.is_stream else len(response.body)
                metrics.http_response_sent(
                    request.method,
                    request.path,
                    response.status,
                    body_size=body_size,
                    duration_ms=elapsed,
                )
