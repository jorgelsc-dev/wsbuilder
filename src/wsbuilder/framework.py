"""Compatibility facade for the original single-file framework module."""

from .app import App, Route, Router
from .constants import DEFAULT_CORS_ALLOW_ORIGIN, MAGIC_WS, STATUS_MESSAGES
from .http import Request, Response, parse_http_request, parse_query_string, send_http_response
from .metrics import AppMetrics, install_metrics
from .cache import Cache, SQLiteMemoryCache, install_cache
from .caches import GlobalCacheRule, ViewResponseCache, install_caches
from .security import ACLRule, SecurityDecision, SecurityPolicy, install_security
from .server import HTTPServer
from .ws import (
    B64_ALPHABET,
    WebSocketConnectionClosedError,
    WebSocketFrame,
    WebSocket,
    WebSocketReadError,
    WebSocketReadTimeoutError,
    WebSocketProtocolError,
    base64_encode,
    handshake_websocket,
    handshake_websocket_with_options,
    is_ws_request,
    make_ws_frame_bytes,
    parse_close_payload,
    read_ws_frame_raw,
    recv_exact,
    sha1,
)

__all__ = [
    "App",
    "HTTPServer",
    "Request",
    "Response",
    "AppMetrics",
    "install_metrics",
    "Cache",
    "SQLiteMemoryCache",
    "install_cache",
    "GlobalCacheRule",
    "ViewResponseCache",
    "install_caches",
    "ACLRule",
    "SecurityDecision",
    "SecurityPolicy",
    "install_security",
    "Route",
    "Router",
    "WebSocket",
    "WebSocketFrame",
    "WebSocketReadError",
    "WebSocketReadTimeoutError",
    "WebSocketConnectionClosedError",
    "WebSocketProtocolError",
    "DEFAULT_CORS_ALLOW_ORIGIN",
    "MAGIC_WS",
    "STATUS_MESSAGES",
    "B64_ALPHABET",
    "base64_encode",
    "handshake_websocket",
    "handshake_websocket_with_options",
    "is_ws_request",
    "make_ws_frame_bytes",
    "parse_close_payload",
    "parse_http_request",
    "parse_query_string",
    "read_ws_frame_raw",
    "recv_exact",
    "send_http_response",
    "sha1",
]
