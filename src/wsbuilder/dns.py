import base64
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

from .logs import get_log

QTYPE_A = 1
QTYPE_NS = 2
QTYPE_CNAME = 5
QTYPE_SOA = 6
QTYPE_PTR = 12
QTYPE_MX = 15
QTYPE_TXT = 16
QTYPE_AAAA = 28
QTYPE_SRV = 33
QTYPE_NAPTR = 35
QTYPE_DS = 43
QTYPE_SSHFP = 44
QTYPE_RRSIG = 46
QTYPE_DNSKEY = 48
QTYPE_TLSA = 52
QTYPE_SVCB = 64
QTYPE_HTTPS = 65
QTYPE_URI = 256
QTYPE_CAA = 257
QTYPE_ANY = 255

QCLASS_IN = 1
QCLASS_CH = 3
QCLASS_HS = 4
QCLASS_NONE = 254
QCLASS_ANY = 255

RCODE_NOERROR = 0
RCODE_FORMERR = 1
RCODE_SERVFAIL = 2
RCODE_NXDOMAIN = 3
RCODE_NOTIMP = 4
RCODE_REFUSED = 5

#: Questions accepted per query; anything else is answered with FORMERR.
MAX_QUESTIONS = 1
#: Classic DNS-over-UDP response size limit (RFC 1035 section 4.2.1).
DNS_UDP_MIN_PAYLOAD = 512
#: Largest UDP payload honoured from an EDNS(0) OPT record (RFC 6891).
DEFAULT_MAX_UDP_PAYLOAD = 1232
FLAG_TC = 0x0200


TYPE_CODES = {
    "A": 1,
    "NS": 2,
    "MD": 3,
    "MF": 4,
    "CNAME": 5,
    "SOA": 6,
    "MB": 7,
    "MG": 8,
    "MR": 9,
    "NULL": 10,
    "WKS": 11,
    "PTR": 12,
    "HINFO": 13,
    "MINFO": 14,
    "MX": 15,
    "TXT": 16,
    "RP": 17,
    "AFSDB": 18,
    "X25": 19,
    "ISDN": 20,
    "RT": 21,
    "NSAP": 22,
    "NSAP-PTR": 23,
    "SIG": 24,
    "KEY": 25,
    "PX": 26,
    "GPOS": 27,
    "AAAA": 28,
    "LOC": 29,
    "NXT": 30,
    "EID": 31,
    "NIMLOC": 32,
    "SRV": 33,
    "ATMA": 34,
    "NAPTR": 35,
    "KX": 36,
    "CERT": 37,
    "A6": 38,
    "DNAME": 39,
    "SINK": 40,
    "OPT": 41,
    "APL": 42,
    "DS": 43,
    "SSHFP": 44,
    "IPSECKEY": 45,
    "RRSIG": 46,
    "NSEC": 47,
    "DNSKEY": 48,
    "DHCID": 49,
    "NSEC3": 50,
    "NSEC3PARAM": 51,
    "TLSA": 52,
    "SMIMEA": 53,
    "HIP": 55,
    "NINFO": 56,
    "RKEY": 57,
    "TALINK": 58,
    "CDS": 59,
    "CDNSKEY": 60,
    "OPENPGPKEY": 61,
    "CSYNC": 62,
    "ZONEMD": 63,
    "SVCB": 64,
    "HTTPS": 65,
    "SPF": 99,
    "UINFO": 100,
    "UID": 101,
    "GID": 102,
    "UNSPEC": 103,
    "NID": 104,
    "L32": 105,
    "L64": 106,
    "LP": 107,
    "EUI48": 108,
    "EUI64": 109,
    "TKEY": 249,
    "TSIG": 250,
    "IXFR": 251,
    "AXFR": 252,
    "MAILB": 253,
    "MAILA": 254,
    "ANY": 255,
    "URI": 256,
    "CAA": 257,
    "AVC": 258,
    "DOA": 259,
    "AMTRELAY": 260,
}

CLASS_CODES = {
    "IN": QCLASS_IN,
    "CH": QCLASS_CH,
    "HS": QCLASS_HS,
    "NONE": QCLASS_NONE,
    "ANY": QCLASS_ANY,
}

NAME_RDATA_TYPES = {
    TYPE_CODES["NS"],
    TYPE_CODES["MD"],
    TYPE_CODES["MF"],
    TYPE_CODES["CNAME"],
    TYPE_CODES["MB"],
    TYPE_CODES["MG"],
    TYPE_CODES["MR"],
    TYPE_CODES["PTR"],
    TYPE_CODES["DNAME"],
}


def _normalize_name(name):
    return (name or "").strip().strip(".").lower()


def _u16(value):
    return int(value).to_bytes(2, byteorder="big", signed=False)


def _u32(value):
    return int(value).to_bytes(4, byteorder="big", signed=False)


def _read_u16(data, offset):
    end = offset + 2
    if end > len(data):
        raise ValueError("invalid DNS packet: short u16")
    return int.from_bytes(data[offset:end], byteorder="big", signed=False), end


def _parse_u8(value, *, field):
    number = int(value)
    if number < 0 or number > 255:
        raise ValueError(f"invalid {field}: {value}")
    return number


def _parse_u16(value, *, field):
    number = int(value)
    if number < 0 or number > 65535:
        raise ValueError(f"invalid {field}: {value}")
    return number


def _parse_u32(value, *, field):
    number = int(value)
    if number < 0 or number > 0xFFFFFFFF:
        raise ValueError(f"invalid {field}: {value}")
    return number


def _parse_dns_type(value):
    if isinstance(value, int):
        return _parse_u16(value, field="rtype")
    text = str(value or "").strip().upper()
    if not text:
        raise ValueError("invalid rtype: empty")
    if text in TYPE_CODES:
        return TYPE_CODES[text]
    if text.startswith("TYPE") and text[4:].isdigit():
        return _parse_u16(int(text[4:]), field="rtype")
    if text.isdigit():
        return _parse_u16(int(text), field="rtype")
    raise ValueError(f"invalid DNS type: {value}")


def _parse_dns_class(value):
    if isinstance(value, int):
        return _parse_u16(value, field="rclass")
    text = str(value or "").strip().upper()
    if not text:
        raise ValueError("invalid rclass: empty")
    if text in CLASS_CODES:
        return CLASS_CODES[text]
    if text.isdigit():
        return _parse_u16(int(text), field="rclass")
    raise ValueError(f"invalid DNS class: {value}")


def _encode_name(name):
    normalized = _normalize_name(name)
    if not normalized:
        return b"\x00"
    chunks = []
    for label in normalized.split("."):
        if not label:
            continue
        raw = label.encode("idna")
        if len(raw) > 63:
            raise ValueError(f"invalid DNS label length for '{label}'")
        chunks.append(bytes([len(raw)]))
        chunks.append(raw)
    chunks.append(b"\x00")
    encoded = b"".join(chunks)
    if len(encoded) > 255:
        raise ValueError(f"invalid DNS name length: {name}")
    return encoded


def _decode_name(data, offset):
    labels = []
    jumped = False
    next_offset = offset
    pointer_hops = 0
    expanded_size = 1

    while True:
        if offset >= len(data):
            raise ValueError("invalid DNS name: out of bounds")

        length = data[offset]
        if length == 0:
            if not jumped:
                next_offset = offset + 1
            break

        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("invalid DNS name: truncated pointer")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if pointer >= len(data):
                raise ValueError("invalid DNS name: bad pointer target")
            if not jumped:
                next_offset = offset + 2
            offset = pointer
            jumped = True
            pointer_hops += 1
            if pointer_hops > 20:
                raise ValueError("invalid DNS name: pointer loop")
            continue

        if length & 0xC0:
            raise ValueError("invalid DNS name: reserved label type")
        if length > 63:
            raise ValueError("invalid DNS name: label too long")

        offset += 1
        end = offset + length
        if end > len(data):
            raise ValueError("invalid DNS name: truncated label")
        expanded_size += length + 1
        if expanded_size > 255:
            raise ValueError("invalid DNS name: name too long")
        try:
            label = data[offset:end].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("invalid DNS name: non-ASCII label") from exc
        labels.append(label)
        offset = end
        if not jumped:
            next_offset = offset

    return ".".join(labels).lower(), next_offset


def _parse_header(data):
    if len(data) < 12:
        raise ValueError("invalid DNS packet: header too short")
    txid = int.from_bytes(data[0:2], byteorder="big", signed=False)
    flags = int.from_bytes(data[2:4], byteorder="big", signed=False)
    counts = tuple(
        int.from_bytes(data[index : index + 2], byteorder="big", signed=False)
        for index in (4, 6, 8, 10)
    )
    return txid, flags, counts


def _parse_questions(data, qdcount, offset=12):
    questions = []
    for _ in range(qdcount):
        qname, offset = _decode_name(data, offset)
        qtype, offset = _read_u16(data, offset)
        qclass, offset = _read_u16(data, offset)
        questions.append((qname, qtype, qclass))
    return questions, offset


def _parse_query(data):
    txid, flags, counts = _parse_header(data)
    questions, _offset = _parse_questions(data, counts[0])
    return txid, flags, questions


def _edns_payload_size(data, offset, counts):
    """UDP payload size advertised by an OPT record, or ``None`` without one."""
    _qdcount, ancount, nscount, arcount = counts
    try:
        for index in range(ancount + nscount + arcount):
            _name, offset = _decode_name(data, offset)
            rtype, offset = _read_u16(data, offset)
            rclass, offset = _read_u16(data, offset)
            offset += 4
            rdlength, offset = _read_u16(data, offset)
            offset += rdlength
            if offset > len(data):
                return None
            if index >= ancount + nscount and rtype == TYPE_CODES["OPT"]:
                return rclass
    except ValueError:
        return None
    return None


def _truncate_response(response, limit):
    """Fit ``response`` in ``limit`` bytes, keeping only the question and setting TC."""
    if len(response) <= limit:
        return response
    try:
        _txid, flags, counts = _parse_header(response)
        _questions, offset = _parse_questions(response, counts[0])
    except ValueError:
        offset = 12
        counts = (0, 0, 0, 0)
        flags = int.from_bytes(response[2:4], byteorder="big", signed=False)
    question_bytes = response[12:offset] if offset <= limit else b""
    qdcount = counts[0] if question_bytes else 0
    return b"".join(
        [
            response[0:2],
            _u16(flags | FLAG_TC),
            _u16(qdcount),
            _u16(0),
            _u16(0),
            _u16(0),
            question_bytes,
        ]
    )


def _build_response(
    txid,
    request_flags,
    questions,
    answers,
    rcode,
    *,
    authoritative=True,
    recursion_available=False,
):
    qr = 0x8000
    aa = 0x0400 if authoritative else 0
    opcode = request_flags & 0x7800
    rd = request_flags & 0x0100
    ra = 0x0080 if recursion_available else 0
    flags = qr | aa | opcode | rd | ra | (rcode & 0x000F)

    packet = [
        _u16(txid),
        _u16(flags),
        _u16(len(questions)),
        _u16(len(answers)),
        _u16(0),
        _u16(0),
    ]
    for qname, qtype, qclass in questions:
        packet.append(_encode_name(qname))
        packet.append(_u16(qtype))
        packet.append(_u16(qclass))

    for row in answers:
        packet.append(_encode_name(row["name"]))
        packet.append(_u16(row["rtype"]))
        packet.append(_u16(row["rclass"]))
        packet.append(_u32(row["ttl"]))
        packet.append(_u16(len(row["rdata"])))
        packet.append(row["rdata"])

    return b"".join(packet)


def _parse_hex_or_base64(raw_value):
    text = str(raw_value or "").strip()
    if text.startswith("0x") or text.startswith("0X"):
        return bytes.fromhex(text[2:])
    return base64.b64decode(text.encode("ascii"), validate=True)


def _as_bytes(value):
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    if value is None:
        return b""
    if isinstance(value, str):
        return value.encode("utf-8")
    raise ValueError(f"unsupported bytes value: {type(value)!r}")


def _pack_ip(ip_value):
    text = str(ip_value).strip()
    try:
        return QTYPE_A, socket.inet_pton(socket.AF_INET, text)
    except OSError:
        pass
    try:
        return QTYPE_AAAA, socket.inet_pton(socket.AF_INET6, text)
    except OSError as exc:
        raise ValueError(f"invalid IP address: {ip_value}") from exc


def _encode_txt(value):
    if isinstance(value, (list, tuple)):
        chunks = value
    else:
        chunks = [value]
    out = []
    for row in chunks:
        raw = _as_bytes(row)
        if len(raw) > 255:
            raise ValueError("TXT segment too long (>255 bytes)")
        out.append(bytes([len(raw)]))
        out.append(raw)
    return b"".join(out)


def _encode_name_rdata(value):
    return _encode_name(str(value))


def _encode_mx(value):
    if isinstance(value, dict):
        preference = value.get("preference", value.get("priority", 10))
        exchange = value.get("exchange", value.get("host"))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        preference, exchange = value
    else:
        preference, exchange = 10, value
    if exchange is None:
        raise ValueError("MX requires 'exchange'")
    return _u16(_parse_u16(preference, field="mx.preference")) + _encode_name(str(exchange))


def _encode_srv(value):
    if isinstance(value, dict):
        priority = value.get("priority", 0)
        weight = value.get("weight", 0)
        port = value.get("port")
        target = value.get("target")
    elif isinstance(value, (list, tuple)) and len(value) == 4:
        priority, weight, port, target = value
    else:
        raise ValueError("SRV value must be dict or 4-item tuple/list")
    if target is None or port is None:
        raise ValueError("SRV requires 'target' and 'port'")
    return (
        _u16(_parse_u16(priority, field="srv.priority"))
        + _u16(_parse_u16(weight, field="srv.weight"))
        + _u16(_parse_u16(port, field="srv.port"))
        + _encode_name(str(target))
    )


def _encode_soa(value):
    if not isinstance(value, dict):
        raise ValueError("SOA value must be a dict")
    mname = value.get("mname")
    rname = value.get("rname")
    if not mname or not rname:
        raise ValueError("SOA requires 'mname' and 'rname'")
    serial = _parse_u32(value.get("serial", 1), field="soa.serial")
    refresh = _parse_u32(value.get("refresh", 3600), field="soa.refresh")
    retry = _parse_u32(value.get("retry", 600), field="soa.retry")
    expire = _parse_u32(value.get("expire", 1209600), field="soa.expire")
    minimum = _parse_u32(value.get("minimum", 60), field="soa.minimum")
    return (
        _encode_name(str(mname))
        + _encode_name(str(rname))
        + _u32(serial)
        + _u32(refresh)
        + _u32(retry)
        + _u32(expire)
        + _u32(minimum)
    )


def _encode_caa(value):
    if isinstance(value, dict):
        flags = _parse_u8(value.get("flags", 0), field="caa.flags")
        tag = str(value.get("tag", "issue"))
        text = str(value.get("value", ""))
    else:
        flags = 0
        tag = "issue"
        text = str(value)
    tag_bytes = tag.encode("ascii")
    if len(tag_bytes) > 255:
        raise ValueError("CAA tag too long")
    return bytes([flags, len(tag_bytes)]) + tag_bytes + text.encode("utf-8")


def _encode_naptr(value):
    if not isinstance(value, dict):
        raise ValueError("NAPTR value must be a dict")
    order = _u16(_parse_u16(value.get("order", 0), field="naptr.order"))
    preference = _u16(_parse_u16(value.get("preference", 0), field="naptr.preference"))
    flags = _encode_txt(value.get("flags", ""))
    services = _encode_txt(value.get("services", ""))
    regexp = _encode_txt(value.get("regexp", ""))
    replacement = _encode_name(str(value.get("replacement", ".")))
    return order + preference + flags + services + regexp + replacement


def _encode_uri(value):
    if isinstance(value, dict):
        priority = _parse_u16(value.get("priority", 0), field="uri.priority")
        weight = _parse_u16(value.get("weight", 0), field="uri.weight")
        target = value.get("target", "")
    elif isinstance(value, (list, tuple)) and len(value) == 3:
        priority = _parse_u16(value[0], field="uri.priority")
        weight = _parse_u16(value[1], field="uri.weight")
        target = value[2]
    else:
        priority = 0
        weight = 0
        target = value
    return _u16(priority) + _u16(weight) + _as_bytes(target)


def _encode_dnskey(value):
    if not isinstance(value, dict):
        raise ValueError("DNSKEY value must be a dict")
    flags = _u16(_parse_u16(value.get("flags", 256), field="dnskey.flags"))
    protocol = bytes([_parse_u8(value.get("protocol", 3), field="dnskey.protocol")])
    algorithm = bytes([_parse_u8(value.get("algorithm", 8), field="dnskey.algorithm")])
    public_key = value.get("public_key", b"")
    if isinstance(public_key, str):
        public_key = _parse_hex_or_base64(public_key)
    else:
        public_key = _as_bytes(public_key)
    return flags + protocol + algorithm + public_key


def _encode_ds(value):
    if not isinstance(value, dict):
        raise ValueError("DS value must be a dict")
    key_tag = _u16(_parse_u16(value.get("key_tag", 0), field="ds.key_tag"))
    algorithm = bytes([_parse_u8(value.get("algorithm", 8), field="ds.algorithm")])
    digest_type = bytes([_parse_u8(value.get("digest_type", 2), field="ds.digest_type")])
    digest = value.get("digest", b"")
    if isinstance(digest, str):
        digest = bytes.fromhex(digest.strip().replace(" ", ""))
    else:
        digest = _as_bytes(digest)
    return key_tag + algorithm + digest_type + digest


def _encode_tlsa(value):
    if not isinstance(value, dict):
        raise ValueError("TLSA value must be a dict")
    usage = bytes([_parse_u8(value.get("usage", 3), field="tlsa.usage")])
    selector = bytes([_parse_u8(value.get("selector", 1), field="tlsa.selector")])
    matching_type = bytes([_parse_u8(value.get("matching_type", 1), field="tlsa.matching_type")])
    cert_data = value.get("cert_data", b"")
    if isinstance(cert_data, str):
        cert_data = bytes.fromhex(cert_data.strip().replace(" ", ""))
    else:
        cert_data = _as_bytes(cert_data)
    return usage + selector + matching_type + cert_data


def _encode_sshfp(value):
    if not isinstance(value, dict):
        raise ValueError("SSHFP value must be a dict")
    algorithm = bytes([_parse_u8(value.get("algorithm", 1), field="sshfp.algorithm")])
    fp_type = bytes([_parse_u8(value.get("fp_type", 2), field="sshfp.fp_type")])
    fingerprint = value.get("fingerprint", b"")
    if isinstance(fingerprint, str):
        fingerprint = bytes.fromhex(fingerprint.strip().replace(" ", ""))
    else:
        fingerprint = _as_bytes(fingerprint)
    return algorithm + fp_type + fingerprint


def _encode_raw(value):
    if isinstance(value, dict):
        if "hex" in value:
            return bytes.fromhex(str(value["hex"]).strip().replace(" ", ""))
        if "base64" in value:
            return base64.b64decode(str(value["base64"]).encode("ascii"), validate=True)
        if "bytes" in value:
            return _as_bytes(value["bytes"])
        if "value" in value:
            return _as_bytes(value["value"])
    return _as_bytes(value)


ENCODERS_BY_TYPE = {
    QTYPE_A: lambda value: socket.inet_pton(socket.AF_INET, str(value).strip()),
    QTYPE_AAAA: lambda value: socket.inet_pton(socket.AF_INET6, str(value).strip()),
    QTYPE_TXT: _encode_txt,
    QTYPE_NS: _encode_name_rdata,
    QTYPE_CNAME: _encode_name_rdata,
    QTYPE_PTR: _encode_name_rdata,
    TYPE_CODES["MB"]: _encode_name_rdata,
    TYPE_CODES["MD"]: _encode_name_rdata,
    TYPE_CODES["MF"]: _encode_name_rdata,
    TYPE_CODES["MG"]: _encode_name_rdata,
    TYPE_CODES["MR"]: _encode_name_rdata,
    TYPE_CODES["DNAME"]: _encode_name_rdata,
    QTYPE_MX: _encode_mx,
    QTYPE_SRV: _encode_srv,
    QTYPE_SOA: _encode_soa,
    QTYPE_CAA: _encode_caa,
    QTYPE_NAPTR: _encode_naptr,
    QTYPE_URI: _encode_uri,
    QTYPE_DNSKEY: _encode_dnskey,
    QTYPE_DS: _encode_ds,
    QTYPE_TLSA: _encode_tlsa,
    QTYPE_SSHFP: _encode_sshfp,
}


def _encode_rdata(rtype, value):
    if isinstance(value, dict) and "rdata" in value:
        return _encode_raw(value["rdata"])
    encoder = ENCODERS_BY_TYPE.get(rtype)
    if encoder is None:
        return _encode_raw(value)
    try:
        return encoder(value)
    except OSError as exc:
        raise ValueError(f"invalid value for type {rtype}: {value}") from exc


def _normalize_upstream(entry):
    host = None
    port = 53
    if isinstance(entry, dict):
        host = entry.get("host")
        port = entry.get("port", 53)
    elif isinstance(entry, (list, tuple)) and len(entry) == 2:
        host, port = entry
    elif isinstance(entry, str):
        text = entry.strip()
        if text.startswith("[") and "]" in text:
            end = text.index("]")
            host = text[1:end]
            rest = text[end + 1 :].strip()
            if rest.startswith(":") and rest[1:].isdigit():
                port = int(rest[1:])
        elif text.count(":") == 1 and text.rsplit(":", 1)[1].isdigit():
            host, raw_port = text.rsplit(":", 1)
            port = int(raw_port)
        else:
            host = text
    else:
        raise ValueError(f"invalid upstream entry: {entry!r}")

    host = str(host or "").strip()
    if not host:
        raise ValueError(f"invalid upstream host: {entry!r}")
    port = _parse_u16(port, field="upstream.port")
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_DGRAM)
    if not infos:
        raise ValueError(f"cannot resolve upstream: {entry!r}")
    family, socktype, proto, _canonname, sockaddr = infos[0]
    return {"host": host, "port": port, "family": family, "socktype": socktype, "proto": proto, "sockaddr": sockaddr}


def _same_upstream_endpoint(upstream, received_addr):
    expected_addr = upstream.get("sockaddr")
    if not isinstance(received_addr, (list, tuple)) or len(received_addr) < 2:
        return False
    if not isinstance(expected_addr, (list, tuple)) or len(expected_addr) < 2:
        return False
    try:
        ports_match = int(received_addr[1]) == int(expected_addr[1])
    except (TypeError, ValueError):
        return False
    if not ports_match:
        return False

    family = upstream.get("family")
    received_host = str(received_addr[0]).split("%", 1)[0]
    expected_host = str(expected_addr[0]).split("%", 1)[0]
    try:
        return socket.inet_pton(family, received_host) == socket.inet_pton(family, expected_host)
    except (OSError, TypeError, ValueError):
        return received_host.lower() == expected_host.lower()


def _is_valid_upstream_response(response, received_addr, upstream, expected_txid, expected_questions):
    if not _same_upstream_endpoint(upstream, received_addr):
        return False
    try:
        txid, flags, questions = _parse_query(response)
    except ValueError:
        return False
    if not (flags & 0x8000):
        return False
    if txid != expected_txid:
        return False
    return questions == expected_questions


class LocalDNSServer:
    """Authoritative DNS UDP server with optional upstream fallback."""

    def __init__(
        self,
        host="127.0.0.1",
        port=5533,
        records=None,
        ttl=60,
        upstream_servers=None,
        upstream_timeout=1.2,
        fallback_to_upstream=None,
        *,
        upstream_workers=4,
        max_pending_upstream=64,
        max_udp_payload=DEFAULT_MAX_UDP_PAYLOAD,
        app=None,
    ):
        self.host = host
        self.port = int(port)
        self.ttl = max(0, int(ttl))
        self.upstream_timeout = max(0.05, float(upstream_timeout))
        self.upstream_workers = max(1, int(upstream_workers))
        self.max_pending_upstream = max(1, int(max_pending_upstream))
        self.max_udp_payload = max(DNS_UDP_MIN_PAYLOAD, min(65535, int(max_udp_payload)))
        self.app = app
        self._sock = None
        self._thread = None
        self._running = threading.Event()
        self._executor = None
        self._executor_lock = threading.Lock()
        self._pending_upstream = 0
        self._records = {}
        self._upstreams = []

        if upstream_servers:
            for row in upstream_servers:
                self._upstreams.append(_normalize_upstream(row))
        if fallback_to_upstream is None:
            self.fallback_to_upstream = bool(self._upstreams)
        else:
            self.fallback_to_upstream = bool(fallback_to_upstream)

        self._install_default_records()
        self._load_records(records)

    def _install_default_records(self):
        self.add_record("localhost", "A", "127.0.0.1")
        self.add_record("localhost", "AAAA", "::1")

    def clear_records(self, keep_defaults=True):
        self._records = {}
        if keep_defaults:
            self._install_default_records()

    def add_raw_record(self, name, rtype, rdata, *, ttl=None, rclass="IN"):
        return self.add_record(name, rtype, {"rdata": rdata}, ttl=ttl, rclass=rclass)

    def add_record(self, name, rtype, value, *, ttl=None, rclass="IN"):
        normalized = _normalize_name(name)
        if not normalized:
            raise ValueError("record name cannot be empty")
        dns_type = _parse_dns_type(rtype)
        dns_class = _parse_dns_class(rclass)
        final_ttl = self.ttl if ttl is None else _parse_u32(ttl, field="ttl")
        rdata = _encode_rdata(dns_type, value)
        row = {
            "name": normalized,
            "rtype": dns_type,
            "rclass": dns_class,
            "ttl": final_ttl,
            "rdata": rdata,
        }
        self._records.setdefault(normalized, [])
        if row not in self._records[normalized]:
            self._records[normalized].append(row)
        return row

    def remove_record(self, name, rtype=None, value=None, rclass=None):
        normalized = _normalize_name(name)
        rows = list(self._records.get(normalized, []))
        if not rows:
            return 0
        type_code = _parse_dns_type(rtype) if rtype is not None else None
        class_code = _parse_dns_class(rclass) if rclass is not None else None
        value_bytes = None
        if value is not None and type_code is not None:
            value_bytes = _encode_rdata(type_code, value)

        keep = []
        removed = 0
        for row in rows:
            if type_code is not None and row["rtype"] != type_code:
                keep.append(row)
                continue
            if class_code is not None and row["rclass"] != class_code:
                keep.append(row)
                continue
            if value_bytes is not None and row["rdata"] != value_bytes:
                keep.append(row)
                continue
            removed += 1

        if keep:
            self._records[normalized] = keep
        else:
            self._records.pop(normalized, None)
        return removed

    def _load_records(self, records):
        if not records:
            return
        if isinstance(records, dict):
            for name, value in records.items():
                self._load_named_records(name, value)
            return
        if isinstance(records, (list, tuple, set)):
            for row in records:
                if not isinstance(row, dict):
                    raise ValueError("list-style records must contain dict entries")
                self._load_flat_record(row)
            return
        raise ValueError("records must be dict or list")

    def _load_named_records(self, name, value):
        normalized = _normalize_name(name)
        if not normalized:
            return

        if isinstance(value, dict):
            maybe_type = value.get("type", value.get("rtype"))
            if maybe_type is not None:
                spec = dict(value)
                spec.setdefault("name", normalized)
                self._load_flat_record(spec)
                return

            default_ttl = value.get("ttl")
            default_class = value.get("class", value.get("rclass", "IN"))
            for key, row_value in value.items():
                type_text = str(key).upper()
                if type_text in ("TTL", "CLASS", "RCLASS", "NAME"):
                    continue
                if type_text not in TYPE_CODES and not type_text.isdigit() and not type_text.startswith("TYPE"):
                    continue
                rows = row_value if isinstance(row_value, (list, tuple, set)) else [row_value]
                for item in rows:
                    if isinstance(item, dict) and ("type" in item or "rtype" in item):
                        spec = dict(item)
                        spec.setdefault("name", normalized)
                        self._load_flat_record(spec)
                        continue
                    ttl = default_ttl
                    rclass = default_class
                    payload = item
                    if isinstance(item, dict):
                        ttl = item.get("ttl", default_ttl)
                        rclass = item.get("class", item.get("rclass", default_class))
                        payload = item.get("value", item.get("rdata", item))
                    self.add_record(
                        normalized,
                        type_text,
                        payload,
                        ttl=ttl,
                        rclass=rclass,
                    )
            return

        values = value if isinstance(value, (list, tuple, set)) else [value]
        for item in values:
            if isinstance(item, dict) and ("type" in item or "rtype" in item):
                spec = dict(item)
                spec.setdefault("name", normalized)
                self._load_flat_record(spec)
                continue
            guessed_type, _ = _pack_ip(item)
            self.add_record(normalized, guessed_type, item)

    def _load_flat_record(self, row):
        name = row.get("name")
        if not name:
            raise ValueError("record entry requires 'name'")
        rtype = row.get("type", row.get("rtype", "A"))
        value = row.get("value")
        if value is None and "rdata" in row:
            value = {"rdata": row.get("rdata")}
        if value is None and "hex" in row:
            value = {"hex": row["hex"]}
        if value is None and "base64" in row:
            value = {"base64": row["base64"]}
        if value is None:
            raise ValueError(f"record entry for '{name}' requires 'value' or 'rdata'")
        self.add_record(
            name,
            rtype,
            value,
            ttl=row.get("ttl"),
            rclass=row.get("class", row.get("rclass", "IN")),
        )

    def _find_records_for_name(self, name):
        normalized = _normalize_name(name)
        rows = self._records.get(normalized)
        if rows:
            return normalized, rows
        labels = normalized.split(".")
        for index in range(1, len(labels)):
            wildcard = "*." + ".".join(labels[index:])
            wildcard_rows = self._records.get(wildcard)
            if wildcard_rows:
                return wildcard, wildcard_rows
        return normalized, []

    def _resolve_single_question(self, qname, qtype, qclass, depth=0):
        if qclass not in (QCLASS_ANY,):
            pass
        name, rows = self._find_records_for_name(qname)
        if not rows:
            return False, []

        if qclass != QCLASS_ANY:
            rows = [row for row in rows if row["rclass"] in (qclass, QCLASS_ANY)]
        if not rows:
            return True, []

        qtype_is_any = qtype == QTYPE_ANY
        matches = [row for row in rows if qtype_is_any or row["rtype"] == qtype]

        cname_rows = [row for row in rows if row["rtype"] == QTYPE_CNAME]
        if not qtype_is_any and qtype != QTYPE_CNAME and cname_rows:
            matches.extend(cname_rows)
            if depth < 4:
                for cname_row in cname_rows:
                    target, _ = _decode_name(cname_row["rdata"] + b"\x00", 0)
                    _exists, chain = self._resolve_single_question(target, qtype, qclass, depth=depth + 1)
                    matches.extend(chain)

        unique = []
        seen = set()
        for row in matches:
            resolved_row = row
            if name.startswith("*.") and row["name"] == name:
                resolved_row = dict(row)
                resolved_row["name"] = _normalize_name(qname)
            key = (
                resolved_row["name"],
                resolved_row["rtype"],
                resolved_row["rclass"],
                resolved_row["ttl"],
                resolved_row["rdata"],
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(resolved_row)

        return True, unique

    def _resolve_questions(self, questions):
        answers = []
        name_exists = False
        unknown_name = False

        for qname, qtype, qclass in questions:
            exists, resolved = self._resolve_single_question(qname, qtype, qclass)
            if exists:
                name_exists = True
                answers.extend(resolved)
            else:
                unknown_name = True

        if answers:
            return RCODE_NOERROR, answers
        if unknown_name and not name_exists:
            return RCODE_NXDOMAIN, []
        return RCODE_NOERROR, []

    def _forward_to_upstream(self, data):
        if not self._upstreams:
            return None
        try:
            expected_txid, _flags, expected_questions = _parse_query(data)
        except ValueError:
            return None
        for upstream in self._upstreams:
            sock = None
            try:
                sock = socket.socket(upstream["family"], upstream["socktype"], upstream["proto"])
                sock.settimeout(self.upstream_timeout)
                sock.sendto(data, upstream["sockaddr"])
                response, received_addr = sock.recvfrom(65535)
                if _is_valid_upstream_response(
                    response,
                    received_addr,
                    upstream,
                    expected_txid,
                    expected_questions,
                ):
                    return response
            except OSError:
                continue
            finally:
                if sock is not None:
                    sock.close()
        return None

    def _response_limit(self, data, offset, counts):
        advertised = _edns_payload_size(data, offset, counts)
        if advertised is None:
            return DNS_UDP_MIN_PAYLOAD
        return max(DNS_UDP_MIN_PAYLOAD, min(int(advertised), self.max_udp_payload))

    def _servfail(self, txid, flags, questions):
        return _build_response(
            txid,
            flags,
            questions,
            [],
            RCODE_SERVFAIL,
            authoritative=False,
            recursion_available=True,
        )

    def _prepare_response(self, data):
        """Answer ``data`` locally.

        Returns ``(response, forward)``: ``response`` is the datagram to send (or
        ``None``) and ``forward`` is ``(txid, flags, questions, limit)`` when the
        query must go to an upstream resolver instead.
        """
        try:
            txid, flags, counts = _parse_header(data)
        except ValueError:
            return None, None
        if flags & 0x8000:
            return None, None
        if counts[0] != MAX_QUESTIONS:
            return _build_response(txid, flags, [], [], RCODE_FORMERR, authoritative=False), None
        try:
            questions, offset = _parse_questions(data, counts[0])
        except ValueError:
            return None, None
        limit = self._response_limit(data, offset, counts)

        if flags & 0x7800:
            response = _build_response(txid, flags, questions, [], RCODE_NOTIMP, authoritative=False)
            return _truncate_response(response, limit), None

        rcode, answers = self._resolve_questions(questions)
        if answers:
            response = _build_response(txid, flags, questions, answers, rcode, authoritative=True)
            return _truncate_response(response, limit), None

        if self.fallback_to_upstream:
            return None, (txid, flags, questions, limit)

        response = _build_response(txid, flags, questions, answers, rcode, authoritative=True)
        return _truncate_response(response, limit), None

    def _forward_response(self, data, forward):
        txid, flags, questions, limit = forward
        upstream_response = self._forward_to_upstream(data)
        if upstream_response is None:
            upstream_response = self._servfail(txid, flags, questions)
        return _truncate_response(upstream_response, limit)

    def _handle_packet(self, data):
        response, forward = self._prepare_response(data)
        if forward is not None:
            return self._forward_response(data, forward)
        return response

    def _send(self, response, addr):
        sock = self._sock
        if sock is None or not response:
            return
        try:
            sock.sendto(response, addr)
        except OSError as exc:
            if self._running.is_set():
                get_log(self.app).warning(
                    "dns.send_error", f"DNS response to {addr!r} not sent: {exc}"
                )

    def _get_executor(self):
        with self._executor_lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.upstream_workers,
                    thread_name_prefix=f"wsbuilder-dns-upstream-{self.port}",
                )
            return self._executor

    def _forward_in_background(self, data, addr, forward):
        with self._executor_lock:
            if self._pending_upstream >= self.max_pending_upstream:
                saturated = True
            else:
                saturated = False
                self._pending_upstream += 1
        if saturated:
            txid, flags, questions, limit = forward
            self._send(_truncate_response(self._servfail(txid, flags, questions), limit), addr)
            return

        def run():
            try:
                self._send(self._forward_response(data, forward), addr)
            except Exception as exc:
                get_log(self.app).exception("dns.upstream_error", "DNS upstream forward failed", exc)

        def release(_future=None):
            with self._executor_lock:
                self._pending_upstream = max(0, self._pending_upstream - 1)

        try:
            future = self._get_executor().submit(run)
        except RuntimeError:
            # The executor was shut down by close(); drop the query.
            release()
            return
        # Runs on completion and on cancellation by close().
        future.add_done_callback(release)

    def _open_socket(self):
        if self._sock is not None:
            return
        infos = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_DGRAM, 0, socket.AI_PASSIVE)
        if not infos:
            raise OSError(f"unable to bind DNS socket: {self.host}:{self.port}")
        family, socktype, proto, _canonname, sockaddr = infos[0]
        sock = socket.socket(family, socktype, proto)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(sockaddr)
        sock.settimeout(0.2)
        self._sock = sock
        self.port = sock.getsockname()[1]

    def serve_forever(self):
        self._open_socket()
        self._running.set()
        try:
            while self._running.is_set():
                sock = self._sock
                if sock is None:
                    break
                try:
                    data, addr = sock.recvfrom(4096)
                except TimeoutError:
                    continue
                except OSError as exc:
                    if not self._running.is_set() or sock.fileno() == -1:
                        break
                    # Per-datagram errors (for example ICMP-driven ECONNREFUSED)
                    # must not stop the service.
                    get_log(self.app).warning("dns.recv_error", f"DNS receive failed: {exc}")
                    continue

                try:
                    response, forward = self._prepare_response(data)
                except Exception:
                    # A malformed datagram must not terminate the DNS service loop.
                    continue
                if forward is not None:
                    self._forward_in_background(data, addr, forward)
                    continue
                self._send(response, addr)
        finally:
            self._running.clear()

    def start(self):
        if self._thread and self._thread.is_alive():
            return self._thread
        self._open_socket()
        self._running.set()
        thread = threading.Thread(
            target=self.serve_forever,
            name=f"wsbuilder-dns-{self.host}:{self.port}",
            daemon=True,
        )
        thread.start()
        self._thread = thread
        return thread

    def close(self):
        self._running.clear()
        with self._executor_lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
