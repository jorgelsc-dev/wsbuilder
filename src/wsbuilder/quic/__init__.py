"""QUIC transport for HTTP/3 (RFC 9000, RFC 9001).

QUIC moves what TCP and TLS did into one protocol over UDP: streams, loss
recovery, flow control and encryption are all part of the same handshake.
That is why HTTP/3 cannot reuse Python's ``ssl``. TLS handshake messages do
not travel in TLS records here -- they ride inside CRYPTO frames, and QUIC
derives its own packet protection keys from the TLS key schedule. CPython
exposes no API for feeding handshake bytes in and pulling secrets out, so
the handshake itself lives in :mod:`wsbuilder.quic.tls` on top of the
primitives ``cryptography`` provides.
"""

from .varint import (
    MAX_VARINT,
    decode_varint,
    encode_varint,
    varint_length,
)

__all__ = [
    "MAX_VARINT",
    "decode_varint",
    "encode_varint",
    "varint_length",
]
