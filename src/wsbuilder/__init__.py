"""wsbuilder: mini framework HTTP + WebSocket."""

import importlib.util

from .bpe import BPEStreamDecoder, BPETokenizer, format_qwen3_chat
from .cache import Cache, SQLiteMemoryCache, install_cache
from .caches import GlobalCacheRule, ViewResponseCache, install_caches
from .codec import CBORDecodeError, CBOREncodeError, CBORSimple, CBORTag, cbor_dumps, cbor_loads
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
from .graph import (
    GRAPH_FORMAT,
    GRAPH_OPS,
    GraphBackend,
    GraphCache,
    GraphFormatError,
    GraphModel,
)
from .graph_convert import convert_to_graph
from .headers import add_header, get_header, has_header, normalize_header_name, set_header
from .ia import (
    AudioData,
    ByteTokenizer,
    DataSet,
    DataSummary,
    DenseLayer,
    ErrorSummary,
    HashingTextVectorizer,
    NeuralNetwork,
    audio_features,
    describe_data,
    evaluate_errors,
    load_wav,
    submit_training_task,
)
from .ia_lm import (
    BYTELM_ARCHITECTURE,
    ByteLanguageModel,
    NativeBackend,
    format_chat,
    submit_lm_training_task,
)
from .llm import (
    DownloadError,
    LanguageModel,
    ModelRecord,
    ModelStore,
    OpenAICompatibleBackend,
    download_file,
    huggingface_files,
    huggingface_url,
    install_llm,
)
from .logs import NDJSONLog, install_logs
from .mdns import LocalMDNSServer, MDNSService, discover_mdns_services
from .metrics import AppMetrics, install_metrics
from .ndjson_model import (
    MODEL_FORMAT,
    ModelFormatError,
    ModelReader,
    ModelTensor,
    ModelWriter,
    is_model_file,
    read_model_header,
)
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
from .rpc import RpcClient, RpcCodec, RpcError, RpcTransportError, rpc_endpoint
from .security import ACLRule, SecurityDecision, SecurityPolicy, install_security
from .weights import (
    GGUFReader,
    SafetensorsReader,
    ShardedSafetensors,
    WeightsFormatError,
    WeightTensor,
    detect_weights_format,
    open_weights,
)

#: Names that need the optional `tls` extra, and the module serving each.
_TLS_EXPORT_MODULES = {
    "CertificateAuthority": "pki",
    "CertificateManager": "pki",
    "MaterializedFiles": "pki",
    "TLSMaterial": "pki",
    "install_tls": "pki",
    "Http3Client": "http3_client",
    "Http3Error": "http3_client",
    "Http3Response": "http3_client",
}
_PKI_EXPORTS = frozenset(_TLS_EXPORT_MODULES)


def _tls_extra_available():
    """Whether the optional ``tls`` extra (cryptography) can be imported."""
    try:
        return importlib.util.find_spec("cryptography") is not None
    except (ImportError, ValueError):
        return False


_TLS_EXTRA_AVAILABLE = _tls_extra_available()


def __getattr__(name):
    """Resolve TLS helpers lazily so cryptography stays an optional install.

    Without the extra, the names raise ``AttributeError`` (with the install
    hint), so ``hasattr()``, ``getattr(..., default)`` and ``inspect`` work.
    """
    if name in _PKI_EXPORTS:
        try:
            module = importlib.import_module(f".{_TLS_EXPORT_MODULES[name]}", __name__)
        except ImportError as exc:
            # Installed but unusable (too old, broken binding): stop
            # advertising the names so later star imports and dir() work.
            global _TLS_EXTRA_AVAILABLE
            _TLS_EXTRA_AVAILABLE = False
            __all__[:] = [item for item in __all__ if item not in _PKI_EXPORTS]
            raise AttributeError(
                f"wsbuilder.{name} needs the 'cryptography' package. "
                "Install it with: pip install wsbuilder[tls]",
                name=name,
            ) from exc
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    names = set(globals())
    if _TLS_EXTRA_AVAILABLE:
        names |= _PKI_EXPORTS
    return sorted(names)

def _read_version():
    """The version setuptools-scm wrote at build time, or the installed one."""
    try:
        from ._version import __version__ as version
    except ImportError:
        # A source tree used without building it (PYTHONPATH=src).
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _dist_version

        try:
            return _dist_version("wsbuilder")
        except PackageNotFoundError:
            return "0.0.0.dev0+unknown"
    return version


__version__ = _read_version()

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
    "add_header",
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
    "AudioData",
    "ByteTokenizer",
    "HashingTextVectorizer",
    "BPETokenizer",
    "BPEStreamDecoder",
    "format_qwen3_chat",
    "audio_features",
    "load_wav",
    "DataSummary",
    "ErrorSummary",
    "DenseLayer",
    "NeuralNetwork",
    "describe_data",
    "evaluate_errors",
    "submit_training_task",
    "Predictor",
    "RpcClient",
    "RpcCodec",
    "RpcError",
    "RpcTransportError",
    "rpc_endpoint",
    "CBORDecodeError",
    "CBOREncodeError",
    "CBORSimple",
    "CBORTag",
    "cbor_dumps",
    "cbor_loads",
    "MODEL_FORMAT",
    "ModelFormatError",
    "ModelReader",
    "ModelTensor",
    "ModelWriter",
    "is_model_file",
    "read_model_header",
    "DownloadError",
    "LanguageModel",
    "BYTELM_ARCHITECTURE",
    "ByteLanguageModel",
    "NativeBackend",
    "format_chat",
    "submit_lm_training_task",
    "GRAPH_FORMAT",
    "GRAPH_OPS",
    "GraphBackend",
    "GraphCache",
    "GraphFormatError",
    "GraphModel",
    "convert_to_graph",
    "ModelRecord",
    "ModelStore",
    "OpenAICompatibleBackend",
    "download_file",
    "huggingface_files",
    "huggingface_url",
    "install_llm",
    "GGUFReader",
    "SafetensorsReader",
    "ShardedSafetensors",
    "WeightTensor",
    "WeightsFormatError",
    "detect_weights_format",
    "open_weights",
]
if _TLS_EXTRA_AVAILABLE:
    # Only advertised when importable, so ``from wsbuilder import *`` works
    # on a core-only install.
    __all__ += sorted(_PKI_EXPORTS)
