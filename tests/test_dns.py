import unittest

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


if __name__ == "__main__":
    unittest.main()
