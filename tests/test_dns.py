import io
import socket
import threading
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
                raise TimeoutError()

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


def _with_counts(query, *, qdcount=None, arcount=None):
    data = bytearray(query)
    if qdcount is not None:
        data[4:6] = int(qdcount).to_bytes(2, byteorder="big")
    if arcount is not None:
        data[10:12] = int(arcount).to_bytes(2, byteorder="big")
    return bytes(data)


def _txt_values(letters):
    return [letter * 250 for letter in letters]


def _opt_record(payload_size):
    return b"\x00" + (41).to_bytes(2, "big") + int(payload_size).to_bytes(2, "big") + b"\x00" * 6


class _LoopSocket:
    """Fake UDP socket feeding datagrams to ``serve_forever``."""

    def __init__(self, dns, packets, *, recv_errors=(), send_errors=0, wait_for=None):
        self.dns = dns
        self.packets = list(packets)
        self.recv_errors = list(recv_errors)
        self.send_errors = send_errors
        self.wait_for = wait_for
        self.sent = []
        self.sent_event = threading.Event()

    def fileno(self):
        return 3

    def close(self):
        return None

    def recvfrom(self, _size):
        if self.recv_errors:
            raise self.recv_errors.pop(0)
        if self.packets:
            return self.packets.pop(0), ("127.0.0.1", 53000)
        if self.wait_for is not None:
            self.wait_for.wait(timeout=5)
        self.dns._running.clear()
        raise TimeoutError()

    def sendto(self, data, addr):
        if self.send_errors:
            self.send_errors -= 1
            raise OSError(90, "Message too long")
        self.sent.append((data, addr))
        self.sent_event.set()


class TestDNSHardening(unittest.TestCase):
    def test_multiple_or_zero_questions_return_formerr(self):
        dns = LocalDNSServer(host="127.0.0.1", port=0)
        query = _build_query("localhost", qtype=1, txid=0x1201)
        question = query[12:]
        many = _with_counts(query[:12] + question * 600, qdcount=600)
        empty = _with_counts(query[:12], qdcount=0)

        for packet in (many, empty):
            response = dns._handle_packet(packet)
            parsed = _parse_response(response)
            self.assertEqual(parsed["rcode"], 1)
            self.assertEqual(parsed["ancount"], 0)
            self.assertEqual(len(response), 12)
            self.assertEqual(parsed["txid"], 0x1201)

    def test_formerr_is_not_sent_for_responses(self):
        dns = LocalDNSServer(host="127.0.0.1", port=0)
        query = _as_response(_with_counts(_build_query("localhost", 1, 0x1202), qdcount=2))
        self.assertIsNone(dns._handle_packet(query))

    def test_large_udp_response_is_truncated_with_tc_bit(self):
        dns = LocalDNSServer(
            host="127.0.0.1", port=0, records={"big.local": {"TXT": _txt_values("abcde")}}
        )
        response = dns._handle_packet(_build_query("big.local", qtype=16, txid=0x1203))
        parsed = _parse_response(response)
        self.assertLessEqual(len(response), 512)
        self.assertTrue(parsed["flags"] & 0x0200)
        self.assertEqual(parsed["ancount"], 0)
        self.assertEqual(int.from_bytes(response[4:6], "big"), 1)

    def test_small_response_has_no_tc_bit(self):
        dns = LocalDNSServer(host="127.0.0.1", port=0)
        parsed = _parse_response(dns._handle_packet(_build_query("localhost", 1, 0x1204)))
        self.assertFalse(parsed["flags"] & 0x0200)
        self.assertEqual(parsed["ancount"], 1)

    def test_edns_payload_size_allows_larger_responses_up_to_limit(self):
        dns = LocalDNSServer(
            host="127.0.0.1", port=0, records={"big.local": {"TXT": _txt_values("abcd")}}
        )
        query = _with_counts(_build_query("big.local", 16, 0x1205), arcount=1) + _opt_record(4096)
        response = dns._handle_packet(query)
        parsed = _parse_response(response)
        self.assertFalse(parsed["flags"] & 0x0200)
        self.assertEqual(parsed["ancount"], 4)
        self.assertLessEqual(len(response), 1232)

        dns.max_udp_payload = 512
        response = dns._handle_packet(query)
        self.assertTrue(_parse_response(response)["flags"] & 0x0200)
        self.assertLessEqual(len(response), 512)

    def test_upstream_response_is_truncated_to_client_limit(self):
        dns = LocalDNSServer(host="127.0.0.1", port=0, fallback_to_upstream=True)
        query = _build_query("remote.example", qtype=16, txid=0x1206)
        big = _as_response(query) + b"\x00" * 900
        with patch.object(dns, "_forward_to_upstream", return_value=big):
            response = dns._handle_packet(query)
        self.assertLessEqual(len(response), 512)
        self.assertTrue(_parse_response(response)["flags"] & 0x0200)

    def test_serve_loop_survives_recv_and_send_errors(self):
        dns = LocalDNSServer(host="127.0.0.1", port=0)
        first = _build_query("localhost", 1, 0x1207)
        second = _build_query("localhost", 1, 0x1208)
        fake = _LoopSocket(
            dns,
            [first, second],
            recv_errors=[OSError(111, "Connection refused")],
            send_errors=1,
        )
        dns._sock = fake
        stream = io.StringIO()
        with patch("wsbuilder.logs.console.stream", stream):
            dns.serve_forever()

        self.assertEqual([int.from_bytes(data[:2], "big") for data, _ in fake.sent], [0x1208])
        output = stream.getvalue()
        self.assertIn("dns.recv_error", output)
        self.assertIn("dns.send_error", output)

    def test_upstream_forwarding_does_not_block_local_answers(self):
        dns = LocalDNSServer(host="127.0.0.1", port=0, fallback_to_upstream=True)
        release = threading.Event()
        remote = _build_query("remote.example", 1, 0x1209)
        local = _build_query("localhost", 1, 0x120A)

        def slow_forward(data):
            release.wait(timeout=5)
            return _as_response(data)

        fake = _LoopSocket(dns, [remote, local], wait_for=release)
        dns._sock = fake
        original_send = fake.sendto

        def sendto(data, addr):
            original_send(data, addr)
            if int.from_bytes(data[:2], "big") == 0x120A:
                release.set()

        fake.sendto = sendto
        with patch.object(dns, "_forward_to_upstream", side_effect=slow_forward):
            dns.serve_forever()
            for _ in range(50):
                if len(fake.sent) == 2:
                    break
                fake.sent_event.clear()
                fake.sent_event.wait(timeout=0.1)
        dns.close()

        txids = [int.from_bytes(data[:2], "big") for data, _ in fake.sent]
        self.assertEqual(txids, [0x120A, 0x1209])

    def test_saturated_upstream_pool_answers_servfail(self):
        dns = LocalDNSServer(
            host="127.0.0.1", port=0, fallback_to_upstream=True, max_pending_upstream=1
        )
        release = threading.Event()
        first = _build_query("one.example", 1, 0x120B)
        second = _build_query("two.example", 1, 0x120C)

        def slow_forward(data):
            release.wait(timeout=5)
            return None

        fake = _LoopSocket(dns, [first, second], wait_for=release)
        dns._sock = fake
        original_send = fake.sendto

        def sendto(data, addr):
            original_send(data, addr)
            if int.from_bytes(data[:2], "big") == 0x120C:
                release.set()

        fake.sendto = sendto
        with patch.object(dns, "_forward_to_upstream", side_effect=slow_forward):
            dns.serve_forever()
            for _ in range(50):
                if len(fake.sent) == 2:
                    break
                fake.sent_event.clear()
                fake.sent_event.wait(timeout=0.1)
        dns.close()

        parsed = [_parse_response(data) for data, _ in fake.sent]
        self.assertEqual([row["txid"] for row in parsed], [0x120C, 0x120B])
        self.assertTrue(all(row["rcode"] == 2 for row in parsed))

    def test_close_shuts_down_upstream_pool(self):
        dns = LocalDNSServer(host="127.0.0.1", port=0, fallback_to_upstream=True)
        executor = dns._get_executor()
        dns.close()
        self.assertIsNone(dns._executor)
        with self.assertRaises(RuntimeError):
            executor.submit(lambda: None)

    def test_real_socket_keeps_answering_after_oversized_query(self):
        dns = LocalDNSServer(
            host="127.0.0.1", port=0, records={"a.test": {"TXT": _txt_values("abcde")}}
        )
        dns.start()
        self.addCleanup(dns.close)
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(client.close)
        client.settimeout(2)
        query = _build_query("a.test", 16, 0x120D)
        attack = _with_counts(query + (b"\xc0\x0c\x00\x10\x00\x01" * 599), qdcount=600)
        client.sendto(attack, ("127.0.0.1", dns.port))
        response, _ = client.recvfrom(65535)
        self.assertEqual(_parse_response(response)["rcode"], 1)
        client.sendto(query, ("127.0.0.1", dns.port))
        response, _ = client.recvfrom(65535)
        self.assertLessEqual(len(response), 512)
        self.assertTrue(_parse_response(response)["flags"] & 0x0200)


if __name__ == "__main__":
    unittest.main()
