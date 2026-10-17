import socket
import unittest
from unittest.mock import patch

from wsbuilder import LocalDNSServer


def _encode_name(name):
    labels = [label for label in name.strip(".").split(".") if label]
    parts = []
    for label in labels:
        raw = label.encode("ascii")
        parts.append(bytes([len(raw)]))
        parts.append(raw)
    parts.append(b"\x00")
    return b"".join(parts)


def _decode_name(data, offset):
    labels = []
    jumped = False
    next_offset = offset
    while True:
        length = data[offset]
        if length == 0:
            if not jumped:
                next_offset = offset + 1
            break
        if length & 0xC0 == 0xC0:
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                next_offset = offset + 2
            offset = pointer
            jumped = True
            continue
        offset += 1
        end = offset + length
        labels.append(data[offset:end].decode("ascii"))
        offset = end
        if not jumped:
            next_offset = offset
    return ".".join(labels).lower(), next_offset


def _build_query(name, qtype, txid):
    header = (
        int(txid).to_bytes(2, byteorder="big", signed=False)
        + (0x0100).to_bytes(2, byteorder="big", signed=False)
        + (1).to_bytes(2, byteorder="big", signed=False)
        + (0).to_bytes(2, byteorder="big", signed=False)
        + (0).to_bytes(2, byteorder="big", signed=False)
        + (0).to_bytes(2, byteorder="big", signed=False)
    )
    return (
        header
        + _encode_name(name)
        + int(qtype).to_bytes(2, byteorder="big", signed=False)
        + (1).to_bytes(2, byteorder="big", signed=False)
    )


def _as_response(query):
    response = bytearray(query)
    flags = int.from_bytes(response[2:4], byteorder="big", signed=False)
    response[2:4] = (flags | 0x8000).to_bytes(2, byteorder="big", signed=False)
    return bytes(response)


def _parse_first_answer(data):
    txid = int.from_bytes(data[0:2], byteorder="big", signed=False)
    flags = int.from_bytes(data[2:4], byteorder="big", signed=False)
    qdcount = int.from_bytes(data[4:6], byteorder="big", signed=False)
    ancount = int.from_bytes(data[6:8], byteorder="big", signed=False)
    offset = 12
    for _ in range(qdcount):
        _qname, offset = _decode_name(data, offset)
        offset += 4

    answer = None
    if ancount:
        name, offset = _decode_name(data, offset)
        atype = int.from_bytes(data[offset : offset + 2], byteorder="big", signed=False)
        aclass = int.from_bytes(data[offset + 2 : offset + 4], byteorder="big", signed=False)
        ttl = int.from_bytes(data[offset + 4 : offset + 8], byteorder="big", signed=False)
        rdlength = int.from_bytes(data[offset + 8 : offset + 10], byteorder="big", signed=False)
        offset += 10
        rdata = data[offset : offset + rdlength]
        answer = {
            "name": name,
            "type": atype,
            "class": aclass,
            "ttl": ttl,
            "rdata": rdata,
        }

    return {
        "txid": txid,
        "flags": flags,
        "rcode": flags & 0x000F,
        "ancount": ancount,
        "answer": answer,
    }


def _parse_response(data):
    txid = int.from_bytes(data[0:2], byteorder="big", signed=False)
    flags = int.from_bytes(data[2:4], byteorder="big", signed=False)
    qdcount = int.from_bytes(data[4:6], byteorder="big", signed=False)
    ancount = int.from_bytes(data[6:8], byteorder="big", signed=False)
    offset = 12
    for _ in range(qdcount):
        _qname, offset = _decode_name(data, offset)
        offset += 4

    answers = []
    for _ in range(ancount):
        name, offset = _decode_name(data, offset)
        atype = int.from_bytes(data[offset : offset + 2], byteorder="big", signed=False)
        aclass = int.from_bytes(data[offset + 2 : offset + 4], byteorder="big", signed=False)
        ttl = int.from_bytes(data[offset + 4 : offset + 8], byteorder="big", signed=False)
        rdlength = int.from_bytes(data[offset + 8 : offset + 10], byteorder="big", signed=False)
        offset += 10
        rdata = data[offset : offset + rdlength]
        offset += rdlength
        answers.append(
            {
                "name": name,
                "type": atype,
                "class": aclass,
                "ttl": ttl,
                "rdata": rdata,
            }
        )

    return {
        "txid": txid,
        "flags": flags,
        "rcode": flags & 0x000F,
        "ancount": ancount,
        "answers": answers,
    }


class TestLocalDNSServer(unittest.TestCase):
    def setUp(self):
        self.server = LocalDNSServer(host="127.0.0.1", port=0)

    def _ask(self, name, qtype, txid):
        query = _build_query(name, qtype, txid=txid)
        data = self.server._handle_packet(query)
        return _parse_first_answer(data)

    def test_localhost_a_record(self):
        response = self._ask("localhost", qtype=1, txid=0x1111)
        self.assertEqual(response["txid"], 0x1111)
        self.assertEqual(response["rcode"], 0)
        self.assertEqual(response["ancount"], 1)
        self.assertEqual(response["answer"]["name"], "localhost")
        self.assertEqual(response["answer"]["type"], 1)
        self.assertEqual(response["answer"]["rdata"], b"\x7f\x00\x00\x01")

    def test_localhost_aaaa_record(self):
        response = self._ask("localhost", qtype=28, txid=0x2222)
        self.assertEqual(response["txid"], 0x2222)
        self.assertEqual(response["rcode"], 0)
        self.assertEqual(response["ancount"], 1)
        self.assertEqual(response["answer"]["type"], 28)
        self.assertEqual(
            response["answer"]["rdata"],
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01",
        )

    def test_unknown_name_returns_nxdomain(self):
        response = self._ask("unknown.local", qtype=1, txid=0x3333)
        self.assertEqual(response["txid"], 0x3333)
        self.assertEqual(response["rcode"], 3)
        self.assertEqual(response["ancount"], 0)

    def test_supports_structured_records_for_mx_txt_srv(self):
        dns = LocalDNSServer(
            host="127.0.0.1",
            port=0,
            records={
                "example.local": {
                    "A": "10.10.0.10",
                    "TXT": "hello",
                    "MX": {"preference": 5, "exchange": "mail.example.local"},
                    "SRV": {"priority": 1, "weight": 10, "port": 8080, "target": "svc.example.local"},
                }
            },
            ttl=120,
        )
        response = _parse_response(dns._handle_packet(_build_query("example.local", qtype=15, txid=0x4444)))
        self.assertEqual(response["rcode"], 0)
        self.assertEqual(response["ancount"], 1)
        self.assertEqual(response["answers"][0]["type"], 15)
        self.assertEqual(response["answers"][0]["ttl"], 120)
        self.assertEqual(response["answers"][0]["rdata"][:2], b"\x00\x05")

        response_txt = _parse_response(dns._handle_packet(_build_query("example.local", qtype=16, txid=0x4445)))
        self.assertEqual(response_txt["answers"][0]["rdata"], b"\x05hello")

        response_srv = _parse_response(dns._handle_packet(_build_query("example.local", qtype=33, txid=0x4446)))
        self.assertEqual(response_srv["answers"][0]["rdata"][:6], b"\x00\x01\x00\x0a\x1f\x90")

    def test_supports_numeric_types_with_raw_rdata(self):
        dns = LocalDNSServer(
            host="127.0.0.1",
            port=0,
            records=[
                {
                    "name": "bin.local",
                    "type": "TYPE65280",
                    "hex": "a1b2c3d4",
                }
            ],
        )
        response = _parse_response(dns._handle_packet(_build_query("bin.local", qtype=65280, txid=0x5555)))
        self.assertEqual(response["rcode"], 0)
        self.assertEqual(response["ancount"], 1)
        self.assertEqual(response["answers"][0]["type"], 65280)
        self.assertEqual(response["answers"][0]["rdata"], bytes.fromhex("a1b2c3d4"))

    def test_wildcard_and_cname_chain_are_resolved(self):
        dns = LocalDNSServer(
            host="127.0.0.1",
            port=0,
            records={
                "*.dev.local": {"A": "127.0.0.2"},
                "api.dev.local": {"CNAME": "target.dev.local"},
                "target.dev.local": {"A": "10.20.30.40"},
            },
        )
        wildcard_response = _parse_response(dns._handle_packet(_build_query("x.dev.local", qtype=1, txid=0x6661)))
        self.assertEqual(wildcard_response["rcode"], 0)
        self.assertEqual(wildcard_response["ancount"], 1)
        self.assertEqual(wildcard_response["answers"][0]["name"], "x.dev.local")
        self.assertEqual(wildcard_response["answers"][0]["type"], 1)
        self.assertEqual(wildcard_response["answers"][0]["rdata"], b"\x7f\x00\x00\x02")

        cname_response = _parse_response(dns._handle_packet(_build_query("api.dev.local", qtype=1, txid=0x6662)))
        self.assertEqual(cname_response["rcode"], 0)
        self.assertGreaterEqual(cname_response["ancount"], 2)
        types = {row["type"] for row in cname_response["answers"]}
        self.assertIn(5, types)
        self.assertIn(1, types)

    def test_upstream_fallback_returns_servfail_when_unreachable(self):
        dns = LocalDNSServer(
            host="127.0.0.1",
            port=0,
            upstream_servers=[{"host": "127.0.0.1", "port": 1}],
            upstream_timeout=0.05,
            fallback_to_upstream=True,
        )
        response = _parse_response(dns._handle_packet(_build_query("unknown.upstream.local", qtype=1, txid=0x7777)))
        self.assertEqual(response["rcode"], 2)
        self.assertEqual(response["ancount"], 0)

    def test_malformed_label_types_and_oversized_names_are_rejected(self):
        header = _build_query("x.local", qtype=1, txid=0x8888)[:12]
        question_tail = b"\x00\x01\x00\x01"
        reserved_label = header + b"\x40" + (b"a" * 64) + b"\x00" + question_tail
        oversized_name = (
            header
            + b"".join(b"\x3f" + (bytes([letter]) * 63) for letter in b"abcd")
            + b"\x00"
            + question_tail
        )

        self.assertIsNone(self.server._handle_packet(reserved_label))
        self.assertIsNone(self.server._handle_packet(oversized_name))

    def test_serve_loop_continues_after_packet_handler_exception(self):
        valid_query = _build_query("localhost", qtype=1, txid=0x8989)
        sent = []
        dns = LocalDNSServer(host="127.0.0.1", port=0)

        class FakeSocket:
            def __init__(self):
                self.packets = [b"malformed", valid_query]

            def recvfrom(self, _size):
                if self.packets:
                    return self.packets.pop(0), ("127.0.0.1", 53000)
                dns._running.clear()
                raise socket.timeout()

            def sendto(self, data, addr):
                sent.append((data, addr))

        dns._sock = FakeSocket()
        original_handler = dns._handle_packet

        def flaky_handler(data):
            if data == b"malformed":
                raise ValueError("bad packet")
            return original_handler(data)

        dns._handle_packet = flaky_handler
        dns.serve_forever()

        self.assertEqual(len(sent), 1)
        self.assertEqual(int.from_bytes(sent[0][0][:2], byteorder="big"), 0x8989)

    def test_upstream_response_must_match_source_transaction_and_question(self):
        dns = LocalDNSServer(host="127.0.0.1", port=0)
        upstream = {
            "host": "192.0.2.53",
            "port": 53,
            "family": socket.AF_INET,
            "socktype": socket.SOCK_DGRAM,
            "proto": socket.IPPROTO_UDP,
            "sockaddr": ("192.0.2.53", 53),
        }
        dns._upstreams = [upstream]
        query = _build_query("example.local", qtype=1, txid=0x9090)
        valid_response = _as_response(query)

        class FakeSocket:
            def __init__(self, response, source):
                self.response = response
                self.source = source

            def settimeout(self, _value):
                return None

            def sendto(self, _data, _addr):
                return None

            def recvfrom(self, _size):
                return self.response, self.source

            def close(self):
                return None

        def forward(response, source=("192.0.2.53", 53)):
            fake = FakeSocket(response, source)
            with patch("wsbuilder.dns.socket.socket", return_value=fake):
                return dns._forward_to_upstream(query)

        wrong_txid = _as_response(_build_query("example.local", qtype=1, txid=0x9191))
        wrong_question = _as_response(_build_query("other.local", qtype=1, txid=0x9090))

        self.assertEqual(forward(valid_response), valid_response)
        self.assertIsNone(forward(valid_response, source=("198.51.100.53", 53)))
        self.assertIsNone(forward(query))
        self.assertIsNone(forward(wrong_txid))
        self.assertIsNone(forward(wrong_question))


if __name__ == "__main__":
    unittest.main()
