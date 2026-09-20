import socket
import ssl
import threading
import time

from .http import Request, Response, parse_http_request, send_http_response
from .http1 import (
    BufferedReader,
    connection_header_value,
    read_chunked_body,
    should_keep_alive,
)
from .ws import _websocket_handshake_error_response, handshake_websocket_with_options, is_ws_request


class HTTPServer:
    MAX_CONNECTION_WORKERS = 64
    MAX_REQUEST_HEADER_BYTES = 64 * 1024
    MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
    ACCEPT_TIMEOUT_SECONDS = 0.5
    ACQUIRE_WORKER_TIMEOUT_SECONDS = 1.0
    REQUEST_READ_TIMEOUT_SECONDS = 10.0
    #: How long a reused connection waits for the next request line.
    KEEPALIVE_TIMEOUT_SECONDS = 5.0
    #: Requests served per connection; 0 means unlimited, 1 disables reuse.
    MAX_KEEPALIVE_REQUESTS = 100

    def __init__(self, host, port, app, ssl_context=None):
        self.host = host
        self.port = port
        self.app = app
        self._sock = None
        self.ssl_context = ssl_context
        self._stop = threading.Event()
        self._serving = threading.Event()
        self.server_address = (host, port)

    def stop(self):
        """Ask a running ``serve_forever`` loop to finish accepting."""
        self._stop.set()

    def wait_until_serving(self, timeout=None):
        """Block until the listening socket is bound, for callers/tests."""
        return self._serving.wait(timeout)

    def _create_listening_socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(128)
        s.settimeout(self.ACCEPT_TIMEOUT_SECONDS)
        return s

    def serve_forever(self):
        for hook in self.app.startup_hooks:
            try:
                hook()
            except Exception as e:
                print(f"[startup] error: {e}")

        s = self._create_listening_socket()
        self._sock = s
        self.server_address = s.getsockname()[:2]
        scheme = "https" if self.ssl_context else "http"
        host, port = self.server_address
        print(f"Server listening on {scheme}://{host}:{port}/")
        worker_limiter = threading.BoundedSemaphore(self.MAX_CONNECTION_WORKERS)
        interrupted = False
        self._serving.set()
        try:
            while not self._stop.is_set():
                try:
                    conn, addr = s.accept()
                except socket.timeout:
                    continue
                except OSError as e:
                    # Transient accept failures (dropped handshake, fd
                    # exhaustion, ...) must not take the whole server down.
                    if self._stop.is_set():
                        break
                    print(f"[accept] error: {e}")
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
            self._serving.clear()
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

    def _reuse_allowed(self):
        return self.MAX_KEEPALIVE_REQUESTS != 1

    def _resolve_ssl_context(self):
        """Resolve the TLS context for one connection.

        Taking a manager or a callable here, rather than one fixed context, is
        what lets a rotating certificate reach new connections without
        restarting the server.
        """
        source = self.ssl_context
        if source is None:
            return None
        provider = getattr(source, "ssl_context", None)
        if callable(provider):
            return provider()
        if callable(source):
            return source()
        return source

    def handle_conn(self, conn, addr):
        try:
            context = self._resolve_ssl_context()
        except Exception as e:
            # A failed rotation (expired authority, unreachable store) must
            # refuse the connection, not kill the worker and strand the socket.
            print(f"[tls] could not resolve a context for {addr}: {e}")
            metrics = getattr(self.app, "metrics", None)
            if metrics:
                metrics.error("tls_context", e)
            try:
                conn.close()
            except Exception:
                pass
            return
        tls_meta = {
            "enabled": context is not None,
            "peer_cert": None,
            "cipher": None,
            "version": None,
        }
        if context is not None:
            try:
                conn = context.wrap_socket(conn, server_side=True)
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
            reader = BufferedReader(conn)
            served = 0
            while True:
                if served:
                    # Idle time between requests is cheaper to give up than a
                    # first read, so a reused connection waits less.
                    try:
                        conn.settimeout(self.KEEPALIVE_TIMEOUT_SECONDS)
                    except Exception:
                        pass
                try:
                    keep_alive = self._serve_one_request(reader, conn, addr, tls_meta)
                except (ConnectionError, OSError):
                    return
                served += 1
                if not keep_alive:
                    return
                if 0 < self.MAX_KEEPALIVE_REQUESTS <= served:
                    return

    def _serve_one_request(self, reader, conn, addr, tls_meta):
        """Serve one message. Returns True when the connection may be reused."""
        metrics = getattr(self.app, "metrics", None)
        security = getattr(self.app, "security", None)
        try:
            conn.settimeout(self.REQUEST_READ_TIMEOUT_SECONDS)
        except Exception:
            pass
        try:
            req = parse_http_request(
                reader,
                max_header_bytes=self.MAX_REQUEST_HEADER_BYTES,
            )
        except socket.timeout:
            send_http_response(conn, Response.text("Request Timeout", status=408))
            return False
        except ValueError as e:
            message = str(e).lower()
            if "too large" in message:
                status = 431
            elif "unsupported http version" in message:
                status = 505
            else:
                status = 400
            send_http_response(conn, Response.text(str(e), status=status))
            return False
        if not req:
            return False

        headers = req["headers"]
        # Anything read past the header block belongs to this body, or to the
        # next pipelined request; the reader owns it either way.
        reader.unread(req["remainder"])
        body = b""
        transfer_encoding = headers.get("transfer-encoding", "").strip()
        content_length = headers.get("content-length")
        expectation = headers.get("expect", "").strip().lower()

        if transfer_encoding and content_length is not None:
            send_http_response(
                conn,
                Response.text(
                    "Content-Length and Transfer-Encoding cannot be combined",
                    status=400,
                ),
            )
            return False
        chunked_request = False
        if transfer_encoding:
            codings = [
                coding.strip().lower()
                for coding in transfer_encoding.split(",")
                if coding.strip()
            ]
            # chunked must be the final coding, and it is the only one this
            # server applies; gzip/deflate request bodies stay unsupported.
            if codings != ["chunked"]:
                send_http_response(
                    conn,
                    Response.text(
                        f"Unsupported Transfer-Encoding: {transfer_encoding}",
                        status=501,
                    ),
                )
                return False
            chunked_request = True
        if expectation and expectation != "100-continue":
            send_http_response(
                conn,
                Response.text("Expectation Failed", status=417),
            )
            return False
        if expectation and content_length is None and not chunked_request:
            send_http_response(
                conn,
                Response.text(
                    "100-continue requires Content-Length",
                    status=417,
                ),
            )
            return False

        trailers = {}
        if chunked_request:
            if expectation == "100-continue":
                try:
                    conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
                except (ConnectionError, OSError):
                    return False
            try:
                body, trailers = read_chunked_body(
                    reader,
                    max_body_bytes=self.MAX_REQUEST_BODY_BYTES,
                )
            except socket.timeout:
                send_http_response(conn, Response.text("Request Timeout", status=408))
                return False
            except (ConnectionError, OSError):
                send_http_response(
                    conn,
                    Response.text("Incomplete Request Body", status=400),
                )
                return False
            except ValueError as e:
                status = 413 if "Too Large" in str(e) else 400
                send_http_response(conn, Response.text(str(e), status=status))
                return False
        elif content_length is not None:
            if not content_length or any(
                char < "0" or char > "9"
                for char in content_length
            ):
                send_http_response(conn, Response.text("Invalid Content-Length", status=400))
                return False
            cl = int(content_length)
            if cl < 0:
                send_http_response(conn, Response.text("Invalid Content-Length", status=400))
                return False
            if cl > self.MAX_REQUEST_BODY_BYTES:
                send_http_response(
                    conn,
                    Response.text("Payload Too Large", status=413),
                )
                return False
            if expectation == "100-continue" and cl > 0:
                try:
                    conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
                except (ConnectionError, OSError):
                    return False
            try:
                body = reader.read_exactly(cl)
            except socket.timeout:
                send_http_response(conn, Response.text("Request Timeout", status=408))
                return False
            except (ConnectionError, OSError):
                send_http_response(
                    conn,
                    Response.text("Incomplete Request Body", status=400),
                )
                return False

        raw_path = req["path"]
        path, _, query = raw_path.partition("?")
        started = time.time()

        try:
            request = Request(
                method=req["method"],
                path=path,
                query_string=query,
                headers=headers,
                body=body,
                client=addr,
                tls=tls_meta,
                version=req["version"],
                trailers=trailers,
            )
        except (TypeError, ValueError) as e:
            send_http_response(
                conn,
                Response.text(f"Invalid request target: {e}", status=400),
            )
            return False
        if metrics:
            metrics.http_request_started(
                request.method,
                request.path,
                body_size=len(request.body),
            )

        ws_request = is_ws_request(headers)
        if ws_request and (
            request.method != "GET" or req["version"] != "HTTP/1.1"
        ):
            response = Response.text(
                "WebSocket upgrade requires GET over HTTP/1.1",
                status=405,
                headers={"Allow": "GET"},
            )
            send_http_response(
                conn,
                response,
                send_body=request.method != "HEAD",
            )
            if metrics:
                elapsed = (time.time() - started) * 1000.0
                metrics.http_response_sent(
                    request.method,
                    request.path,
                    response.status,
                    body_size=(
                        len(response.body)
                        if request.method != "HEAD"
                        else 0
                    ),
                    duration_ms=elapsed,
                )
            if security:
                security.observe_response(request, response.status)
            return False

        if ws_request and security:
            decision = security.evaluate(request)
            if not decision.allowed:
                response = decision.to_response()
                send_http_response(
                    conn,
                    response,
                    send_body=request.method != "HEAD",
                )
                if metrics:
                    elapsed = (time.time() - started) * 1000.0
                    metrics.http_response_sent(
                        request.method,
                        request.path,
                        response.status,
                        body_size=len(response.body),
                        duration_ms=elapsed,
                    )
                security.observe_response(request, response.status)
                return False

        if ws_request:
            ws_route = self.app.ws_routes.get(path)
            if not ws_route:
                response = Response.text("Not Found", status=404)
                send_http_response(
                    conn,
                    response,
                    send_body=request.method != "HEAD",
                )
                if metrics:
                    elapsed = (time.time() - started) * 1000.0
                    metrics.http_response_sent(
                        request.method,
                        request.path,
                        response.status,
                        body_size=len(response.body),
                        duration_ms=elapsed,
                    )
                if security:
                    security.observe_response(request, response.status)
                return False
            handshake_error = _websocket_handshake_error_response(headers)
            if handshake_error is not None:
                send_http_response(
                    conn,
                    handshake_error,
                    send_body=request.method != "HEAD",
                )
                if metrics:
                    elapsed = (time.time() - started) * 1000.0
                    metrics.error("ws_handshake", handshake_error.status)
                    metrics.http_response_sent(
                        request.method,
                        request.path,
                        handshake_error.status,
                        body_size=len(handshake_error.body),
                        duration_ms=elapsed,
                    )
                if security:
                    security.observe_response(request, handshake_error.status)
                return False
            ws = handshake_websocket_with_options(
                conn,
                addr,
                headers,
                supported_subprotocols=ws_route.get("subprotocols", ()),
                idle_timeout=ws_route.get("idle_timeout", 0.0),
                keepalive_interval=ws_route.get("keepalive_interval", 0.0),
                pong_timeout=ws_route.get("pong_timeout", 0.0),
                auto_pong=ws_route.get("auto_pong", True),
                on_close=ws_route.get("on_close"),
                on_error=ws_route.get("on_error"),
                on_timeout=ws_route.get("on_timeout"),
                io_poll_interval=ws_route.get("io_poll_interval", 1.0),
                ping_payload=ws_route.get("ping_payload", b""),
            )
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
                if security:
                    security.observe_response(request, 400)
                return False
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
            if security:
                security.observe_response(request, 101)
            try:
                ws_route["handler"](ws, request)
            except Exception as e:
                print(f"[ws] error: {e}")
                if metrics:
                    metrics.error("ws_handler", e)
            finally:
                if metrics:
                    metrics.ws_closed(path)
            return False

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
            if security:
                security.observe_response(request, 500)
            send_http_response(conn, Response.text("Internal Server Error", status=500))
            return False

        send_body = request.method != "HEAD"
        keep_alive = should_keep_alive(
            request.version,
            headers,
            response,
            send_body=send_body,
            server_allows=self._reuse_allowed(),
        )
        explicit = connection_header_value(request.version, keep_alive)
        if explicit is not None:
            response.headers.setdefault("Connection", explicit)

        try:
            send_http_response(
                conn,
                response,
                send_body=send_body,
                keep_alive=keep_alive,
            )
        except ValueError as e:
            if metrics:
                metrics.error("http_response", e)
            response = Response.text("Internal Server Error", status=500)
            keep_alive = False
            send_http_response(
                conn,
                response,
                send_body=send_body,
            )
        if metrics:
            elapsed = (time.time() - started) * 1000.0
            body_size = (
                0
                if response.is_stream or request.method == "HEAD"
                else len(response.body)
            )
            metrics.http_response_sent(
                request.method,
                request.path,
                response.status,
                body_size=body_size,
                duration_ms=elapsed,
            )
        if security:
            security.observe_response(request, response.status)
        return keep_alive
