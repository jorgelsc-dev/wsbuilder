import socket
import threading


QTYPE_A = 1
QTYPE_AAAA = 28
QTYPE_ANY = 255
QCLASS_IN = 1
QCLASS_ANY = 255

RCODE_NOERROR = 0
RCODE_NXDOMAIN = 3
RCODE_NOTIMP = 4


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


def _encode_name(name):
    normalized = _normalize_name(name)
    if not normalized:
        return b"\x00"
    chunks = []
    for label in normalized.split("."):
        raw = label.encode("ascii", errors="ignore")
        chunks.append(bytes([len(raw)]))
        chunks.append(raw)
    chunks.append(b"\x00")
    return b"".join(chunks)


def _decode_name(data, offset):
    labels = []
    jumped = False
    next_offset = offset
    pointer_hops = 0

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

        offset += 1
        end = offset + length
        if end > len(data):
            raise ValueError("invalid DNS name: truncated label")
        labels.append(data[offset:end].decode("ascii", errors="ignore"))
        offset = end
        if not jumped:
            next_offset = offset

    return ".".join(labels).lower(), next_offset


def _parse_query(data):
    if len(data) < 12:
        raise ValueError("invalid DNS packet: header too short")

    txid = int.from_bytes(data[0:2], byteorder="big", signed=False)
    flags = int.from_bytes(data[2:4], byteorder="big", signed=False)
    qdcount = int.from_bytes(data[4:6], byteorder="big", signed=False)
    offset = 12
    questions = []
    for _ in range(qdcount):
        qname, offset = _decode_name(data, offset)
        qtype, offset = _read_u16(data, offset)
        qclass, offset = _read_u16(data, offset)
        questions.append((qname, qtype, qclass))

    return txid, flags, questions


def _build_response(txid, request_flags, questions, answers, rcode):
    qr = 0x8000
    aa = 0x0400
    opcode = request_flags & 0x7800
    rd = request_flags & 0x0100
    flags = qr | aa | opcode | rd | (rcode & 0x000F)

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

    for name, rtype, rclass, ttl, rdata in answers:
        packet.append(_encode_name(name))
        packet.append(_u16(rtype))
        packet.append(_u16(rclass))
        packet.append(_u32(ttl))
        packet.append(_u16(len(rdata)))
        packet.append(rdata)

    return b"".join(packet)


def _pack_ip(ip_value):
    try:
        return 4, socket.inet_pton(socket.AF_INET, ip_value)
    except OSError:
        pass
    try:
        return 6, socket.inet_pton(socket.AF_INET6, ip_value)
    except OSError as exc:
        raise ValueError(f"invalid IP address: {ip_value}") from exc


class LocalDNSServer:
    """Minimal UDP DNS server for local hostnames."""

    def __init__(self, host="127.0.0.53", port=53, records=None, ttl=60):
        self.host = host
        self.port = int(port)
        self.ttl = max(0, int(ttl))
        self._sock = None
        self._thread = None
        self._running = threading.Event()

        self._a_records = {}
        self._aaaa_records = {}
        self._load_records(records)

    def _load_records(self, records):
        merged = {"localhost": ["127.0.0.1", "::1"]}
        if records:
            for name, values in records.items():
                normalized = _normalize_name(name)
                if not normalized:
                    continue
                if isinstance(values, (list, tuple, set)):
                    merged.setdefault(normalized, [])
                    merged[normalized].extend(values)
                else:
                    merged.setdefault(normalized, [])
                    merged[normalized].append(values)

        for name, values in merged.items():
            for value in values:
                version, packed = _pack_ip(value)
                if version == 4:
                    self._a_records.setdefault(name, [])
                    if packed not in self._a_records[name]:
                        self._a_records[name].append(packed)
                elif version == 6:
                    self._aaaa_records.setdefault(name, [])
                    if packed not in self._aaaa_records[name]:
                        self._aaaa_records[name].append(packed)

    def _resolve_questions(self, questions):
        answers = []
        unknown_name = False
        for qname, qtype, qclass in questions:
            if qclass not in (QCLASS_IN, QCLASS_ANY):
                continue

            name = _normalize_name(qname)
            has_name = name in self._a_records or name in self._aaaa_records
            if not has_name:
                unknown_name = True
                continue

            if qtype in (QTYPE_A, QTYPE_ANY):
                for packed in self._a_records.get(name, []):
                    answers.append((qname, QTYPE_A, QCLASS_IN, self.ttl, packed))

            if qtype in (QTYPE_AAAA, QTYPE_ANY):
                for packed in self._aaaa_records.get(name, []):
                    answers.append((qname, QTYPE_AAAA, QCLASS_IN, self.ttl, packed))

        if not answers and unknown_name:
            return RCODE_NXDOMAIN, answers
        return RCODE_NOERROR, answers

    def _handle_packet(self, data):
        try:
            txid, flags, questions = _parse_query(data)
        except ValueError:
            return None

        is_response = bool(flags & 0x8000)
        opcode = flags & 0x7800
        if is_response:
            return None
        if opcode:
            return _build_response(txid, flags, questions, [], RCODE_NOTIMP)

        rcode, answers = self._resolve_questions(questions)
        return _build_response(txid, flags, questions, answers, rcode)

    def _open_socket(self):
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.settimeout(0.2)
        self._sock = sock
        self.port = sock.getsockname()[1]

    def serve_forever(self):
        self._open_socket()
        self._running.set()
        try:
            while self._running.is_set():
                try:
                    data, addr = self._sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    if self._running.is_set():
                        raise
                    break

                response = self._handle_packet(data)
                if response:
                    try:
                        self._sock.sendto(response, addr)
                    except OSError:
                        if self._running.is_set():
                            raise
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
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
