"""QUIC packet protection and the TLS 1.3 key schedule it borrows (RFC 9001).

QUIC does not wrap TLS records. It takes the key schedule of TLS 1.3 and
applies it to its own packets: an AEAD over the payload, plus a second layer
that masks the parts of the header an observer could otherwise use to follow
a connection across paths.

Initial packets are a special case. They are protected with keys derived from
a salt fixed by the specification and the client's chosen connection id, so
both endpoints can read each other before any handshake has happened. That
protects against off-path injection, not against anyone who can read the
packet -- the salt is public.
"""

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
from cryptography.hazmat.primitives.hmac import HMAC
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

#: RFC 9001 section 5.2, for QUIC version 1.
INITIAL_SALT_V1 = bytes.fromhex("38762cf7f55934b34d179ae6a4c80cadccbb7f0a")
VERSION_1 = 0x00000001

#: The retry integrity tag key and nonce of RFC 9001 section 5.8.
RETRY_INTEGRITY_KEY_V1 = bytes.fromhex("be0c690b9f66575a1d766b54e368c84e")
RETRY_INTEGRITY_NONCE_V1 = bytes.fromhex("461599d35d632bf2239825bb")

AEAD_TAG_SIZE = 16
SAMPLE_SIZE = 16


def hkdf_extract(salt, key_material, algorithm=None):
    """HKDF-Extract (RFC 5869 section 2.2)."""
    algorithm = algorithm or hashes.SHA256()
    mac = HMAC(bytes(salt), algorithm)
    mac.update(bytes(key_material))
    return mac.finalize()


def hkdf_expand_label(secret, label, context, length, algorithm=None):
    """HKDF-Expand-Label of TLS 1.3 (RFC 8446 section 7.1)."""
    algorithm = algorithm or hashes.SHA256()
    full_label = b"tls13 " + (label if isinstance(label, bytes) else label.encode("ascii"))
    info = (
        int(length).to_bytes(2, "big")
        + bytes([len(full_label)])
        + full_label
        + bytes([len(context)])
        + bytes(context)
    )
    return HKDFExpand(algorithm=algorithm, length=int(length), info=info).derive(bytes(secret))


def initial_secrets(destination_connection_id, *, salt=INITIAL_SALT_V1):
    """Derive the client and server Initial secrets from the client's DCID."""
    initial = hkdf_extract(salt, destination_connection_id)
    return (
        hkdf_expand_label(initial, "client in", b"", 32),
        hkdf_expand_label(initial, "server in", b"", 32),
    )


class PacketKeys:
    """The key, iv and header-protection key one direction uses."""

    __slots__ = ("key", "iv", "hp", "aead", "cipher_name")

    def __init__(self, secret, cipher_name="aes-128-gcm", algorithm=None):
        algorithm = algorithm or hashes.SHA256()
        sizes = {"aes-128-gcm": 16, "aes-256-gcm": 32, "chacha20-poly1305": 32}
        if cipher_name not in sizes:
            raise ValueError(f"unsupported AEAD {cipher_name!r}")
        key_size = sizes[cipher_name]
        self.cipher_name = cipher_name
        self.key = hkdf_expand_label(secret, "quic key", b"", key_size, algorithm)
        self.iv = hkdf_expand_label(secret, "quic iv", b"", 12, algorithm)
        self.hp = hkdf_expand_label(secret, "quic hp", b"", key_size, algorithm)
        self.aead = (
            ChaCha20Poly1305(self.key)
            if cipher_name == "chacha20-poly1305"
            else AESGCM(self.key)
        )

    def nonce(self, packet_number):
        """The per-packet nonce: the iv with the packet number XORed in."""
        counter = int(packet_number).to_bytes(len(self.iv), "big")
        return bytes(a ^ b for a, b in zip(self.iv, counter))

    def seal(self, packet_number, header, payload):
        return self.aead.encrypt(self.nonce(packet_number), bytes(payload), bytes(header))

    def open(self, packet_number, header, ciphertext):
        return self.aead.decrypt(self.nonce(packet_number), bytes(ciphertext), bytes(header))

    def header_mask(self, sample):
        """Five mask octets derived from a sample of the protected payload."""
        sample = bytes(sample)
        if len(sample) != SAMPLE_SIZE:
            raise ValueError("the header protection sample is 16 octets")
        if self.cipher_name == "chacha20-poly1305":
            # RFC 9001 section 5.4.4: the 16-octet sample is the counter and
            # the nonce, which is exactly the layout ChaCha20 wants here.
            cipher = Cipher(algorithms.ChaCha20(self.hp, sample), mode=None)
            return cipher.encryptor().update(b"\x00" * 5)
        encryptor = Cipher(algorithms.AES(self.hp), modes.ECB()).encryptor()
        return (encryptor.update(sample) + encryptor.finalize())[:5]


def apply_header_protection(keys, packet, pn_offset, pn_length):
    """Mask the reserved bits, the packet number length and the number itself."""
    packet = bytearray(packet)
    sample_offset = pn_offset + 4
    mask = keys.header_mask(packet[sample_offset : sample_offset + SAMPLE_SIZE])
    # Long headers expose four bits to protection, short headers five.
    packet[0] ^= mask[0] & (0x0F if packet[0] & 0x80 else 0x1F)
    for index in range(pn_length):
        packet[pn_offset + index] ^= mask[1 + index]
    return bytes(packet)


def remove_header_protection(keys, packet, pn_offset):
    """Undo header protection, returning ``(packet, packet_number, length)``."""
    packet = bytearray(packet)
    sample_offset = pn_offset + 4
    if sample_offset + SAMPLE_SIZE > len(packet):
        raise ValueError("packet too short to carry a header protection sample")
    mask = keys.header_mask(packet[sample_offset : sample_offset + SAMPLE_SIZE])
    packet[0] ^= mask[0] & (0x0F if packet[0] & 0x80 else 0x1F)
    pn_length = (packet[0] & 0x03) + 1
    for index in range(pn_length):
        packet[pn_offset + index] ^= mask[1 + index]
    number = int.from_bytes(bytes(packet[pn_offset : pn_offset + pn_length]), "big")
    return bytes(packet), number, pn_length


def decode_packet_number(truncated, pn_length, largest_acknowledged):
    """Recover the full packet number from its truncated form (RFC 9000 A.3)."""
    window = 1 << (pn_length * 8)
    half = window // 2
    expected = largest_acknowledged + 1
    candidate = (expected & ~(window - 1)) | truncated
    if candidate <= expected - half and candidate < (1 << 62) - window:
        return candidate + window
    if candidate > expected + half and candidate >= window:
        return candidate - window
    return candidate


def retry_integrity_tag(original_destination_connection_id, retry_packet):
    """The tag that proves a Retry came from someone on the path."""
    pseudo = (
        bytes([len(original_destination_connection_id)])
        + bytes(original_destination_connection_id)
        + bytes(retry_packet)
    )
    aead = AESGCM(RETRY_INTEGRITY_KEY_V1)
    return aead.encrypt(RETRY_INTEGRITY_NONCE_V1, b"", pseudo)


__all__ = [
    "AEAD_TAG_SIZE",
    "INITIAL_SALT_V1",
    "PacketKeys",
    "SAMPLE_SIZE",
    "VERSION_1",
    "apply_header_protection",
    "decode_packet_number",
    "hkdf_expand_label",
    "hkdf_extract",
    "initial_secrets",
    "remove_header_protection",
    "retry_integrity_tag",
]
