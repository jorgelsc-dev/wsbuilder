"""wsbuilder: mini framework HTTP + WebSocket."""

from .cache import Cache, SQLiteMemoryCache, install_cache
from .caches import GlobalCacheRule, ViewResponseCache, install_caches
from .cookies import build_set_cookie, get_cookie, parse_cookie_header
from .db_replicas import (
    DatabaseReplica,
    DatabaseReplicaPool,
    OptimizedDatabase,
    SQLite3OptimizationConfig,
)
from .dns import LocalDNSServer
from .framework import (
    BALANCING_BEST,
    BALANCING_CONSISTENT_HASH,
    BALANCING_FIRST_AVAILABLE,
    BALANCING_IP_HASH,
    BALANCING_LEAST_BYTES_IN,
    BALANCING_LEAST_BYTES_OUT,
    BALANCING_LEAST_CONNECTIONS,
    BALANCING_LEAST_REQUESTS,
    BALANCING_LEAST_RESPONSE_TIME,
    BALANCING_POWER_OF_TWO_CHOICES,
    BALANCING_RANDOM,
    BALANCING_ROUND_ROBIN,
    BALANCING_WEIGHTED_ROUND_ROBIN,
    SUPPORTED_BALANCING_STRATEGIES,
    TASK_CANCELLED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_REJECTED,
    TASK_RUNNING,
    App,
    HTTPServer,
    ProxyI,
    ProxyMetricsBucket,
    ProxyRouteBuilder,
    ProxyRule,
    ProxyTarget,
    Request,
    Response,
    Route,
    Router,
    RunningStats,
    TaskCancelledError,
    TaskClosedError,
    TaskContext,
    TaskError,
    TaskHandle,
    TaskManager,
    TaskRejectedError,
    WebSocket,
    handshake_websocket_with_options,
    install_proxyi,
    normalize_balance_mode,
    normalize_target,
    parse_close_payload,
    parse_query_string,
    proxyi,
)
from .headers import get_header, has_header, normalize_header_name, set_header
from .ia import (
    DataSet,
    DataSummary,
    DenseLayer,
    ErrorSummary,
    NeuralNetwork,
    describe_data,
    evaluate_errors,
    submit_training_task,
)
from .logs import NDJSONLog, install_logs
from .mdns import LocalMDNSServer, MDNSService, discover_mdns_services
from .metrics import AppMetrics, install_metrics
from .orm import (
    SQL,
    BlobField,
    BooleanField,
    Database,
    DateTimeField,
    Field,
    IntegerField,
    JSONField,
    Model,
    QuerySet,
    RealField,
    TextField,
    Transaction,
    create_tables,
    drop_tables,
    quote_identifier,
    validate_identifier,
)
from .predicts import Predictor
from .security import ACLRule, SecurityDecision, SecurityPolicy, install_security

#: Names served from wsbuilder.pki, which needs the optional `tls` extra.
_PKI_EXPORTS = frozenset(
    {
        "CertificateAuthority",
        "CertificateManager",
        "MaterializedFiles",
        "TLSMaterial",
        "install_tls",
    }
)


def __getattr__(name):
    """Resolve TLS helpers lazily so cryptography stays an optional install."""
    if name in _PKI_EXPORTS:
        try:
            from . import pki
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise ImportError(
                f"wsbuilder.{name} needs the 'cryptography' package. "
                "Install it with: pip install wsbuilder[tls]"
            ) from exc
        value = getattr(pki, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | _PKI_EXPORTS)

__version__ = "1.0.2"

__all__ = [
    "App",
    "HTTPServer",
    "Request",
    "Response",
    "Route",
    "Router",
    "WebSocket",
    "TaskError",
    "TaskClosedError",
    "TaskCancelledError",
    "TaskRejectedError",
    "TaskContext",
    "TaskHandle",
    "TaskManager",
    "TASK_PENDING",
    "TASK_RUNNING",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "TASK_CANCELLED",
    "TASK_REJECTED",
    "ProxyI",
    "proxyi",
    "ProxyRule",
    "ProxyRouteBuilder",
    "ProxyTarget",
    "ProxyMetricsBucket",
    "RunningStats",
    "install_proxyi",
    "normalize_balance_mode",
    "normalize_target",
    "SUPPORTED_BALANCING_STRATEGIES",
    "BALANCING_ROUND_ROBIN",
    "BALANCING_WEIGHTED_ROUND_ROBIN",
    "BALANCING_RANDOM",
    "BALANCING_LEAST_CONNECTIONS",
    "BALANCING_LEAST_RESPONSE_TIME",
    "BALANCING_LEAST_REQUESTS",
    "BALANCING_LEAST_BYTES_IN",
    "BALANCING_LEAST_BYTES_OUT",
    "BALANCING_IP_HASH",
    "BALANCING_CONSISTENT_HASH",
    "BALANCING_FIRST_AVAILABLE",
    "BALANCING_POWER_OF_TWO_CHOICES",
    "BALANCING_BEST",
    "LocalDNSServer",
    "LocalMDNSServer",
    "MDNSService",
    "discover_mdns_services",
    "normalize_header_name",
    "get_header",
    "has_header",
    "set_header",
    "parse_cookie_header",
    "get_cookie",
    "build_set_cookie",
    "AppMetrics",
    "install_metrics",
    "NDJSONLog",
    "install_logs",
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
    "Database",
    "Model",
    "QuerySet",
    "Transaction",
    "Field",
    "IntegerField",
    "TextField",
    "RealField",
    "BlobField",
    "BooleanField",
    "DateTimeField",
    "JSONField",
    "SQL",
    "create_tables",
    "drop_tables",
    "quote_identifier",
    "validate_identifier",
    "parse_close_payload",
    "handshake_websocket_with_options",
    "parse_query_string",
    "DatabaseReplica",
    "DatabaseReplicaPool",
    "OptimizedDatabase",
    "SQLite3OptimizationConfig",
    "DataSet",
    "DataSummary",
    "ErrorSummary",
    "DenseLayer",
    "NeuralNetwork",
    "describe_data",
    "evaluate_errors",
    "submit_training_task",
    "Predictor",
    "CertificateAuthority",
    "CertificateManager",
    "MaterializedFiles",
    "TLSMaterial",
    "install_tls",
]
