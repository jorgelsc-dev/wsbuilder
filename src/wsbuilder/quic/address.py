"""Address validation and path validation for QUIC (RFC 9000 sections 8 and 9).

Two problems share one shape here, and it helps to keep them apart.

*Address validation* answers "did this client really come from the address it
claims?", before the handshake. A server that answers a spoofed Initial with
a large flight becomes an amplifier pointed at the victim, so RFC 9000 caps
what it may send to three times what it received -- and a Retry token lets it
skip that cap on a later attempt by proving the client can receive at that
address.

*Path validation* answers the same question after the connection exists,
when packets start arriving from somewhere new. That is connection
migration: the address changed but the connection did not, which is the
property QUIC has and TCP does not. A PATH_CHALLENGE with unpredictable data
is what proves the new path works before anything is committed to it.
"""

import hmac
import os
import time
from hashlib import sha256

#: RFC 9000 section 8: until validated, a server may send no more than this
#: multiple of what it has received.
AMPLIFICATION_LIMIT = 3
TOKEN_LIFETIME_SECONDS = 600.0
PATH_CHALLENGE_SIZE = 8


class AddressValidator:
    """Issues and checks the Retry tokens that prove an address is reachable.

    The token is not a secret handed to a trusted party: it is a MAC over
    what the server wants to remember, so a forged one fails and a replayed
    one from another address fails too.
    """

    def __init__(self, secret=None, lifetime=TOKEN_LIFETIME_SECONDS, time_source=None):
        self.secret = bytes(secret) if secret else os.urandom(32)
        self.lifetime = float(lifetime)
        self._now = time_source or time.time

    @staticmethod
    def _address_bytes(address):
        host, port = address
        return f"{host}|{int(port)}".encode("utf-8")

    def issue(self, address, original_destination_cid):
        """Mint a token tied to this address and the client's first id."""
        issued = int(self._now())
        body = (
            issued.to_bytes(8, "big")
            + bytes([len(original_destination_cid)])
            + bytes(original_destination_cid)
        )
        tag = hmac.new(
            self.secret, body + self._address_bytes(address), sha256
        ).digest()[:16]
        return body + tag

    def validate(self, token, address):
        """Return the original connection id a valid token carries, else None."""
        token = bytes(token or b"")
        if len(token) < 8 + 1 + 16:
            return None
        issued = int.from_bytes(token[:8], "big")
        cid_length = token[8]
        body_end = 9 + cid_length
        if len(token) != body_end + 16:
            return None
        body, tag = token[:body_end], token[body_end:]
        expected = hmac.new(
            self.secret, body + self._address_bytes(address), sha256
        ).digest()[:16]
        if not hmac.compare_digest(tag, expected):
            return None
        if self.lifetime > 0 and self._now() - issued > self.lifetime:
            return None
        return token[9:body_end]


class AmplificationLimit:
    """Caps what may be sent to an address that has not proved it exists."""

    def __init__(self, factor=AMPLIFICATION_LIMIT):
        self.factor = int(factor)
        self.received = 0
        self.sent = 0
        self.validated = False

    def on_received(self, size):
        self.received += int(size)

    def on_sent(self, size):
        self.sent += int(size)

    def budget(self):
        if self.validated:
            return None  # No cap once the address is known to be reachable.
        return max(0, self.received * self.factor - self.sent)

    def may_send(self, size):
        budget = self.budget()
        return budget is None or int(size) <= budget

    def validate(self):
        self.validated = True

    def describe(self):
        return {
            "validated": self.validated,
            "received": self.received,
            "sent": self.sent,
            "budget": self.budget(),
        }


class PathValidator:
    """Tracks the challenge outstanding on a path a peer has moved to."""

    def __init__(self, time_source=None):
        self._now = time_source or time.monotonic
        self.challenges = {}
        self.validated_paths = set()
        self.migrations = 0

    def challenge(self, address):
        """Start validating a path, returning the data to send."""
        data = os.urandom(PATH_CHALLENGE_SIZE)
        self.challenges[data] = (tuple(address), self._now())
        return data

    def on_response(self, data, address):
        """A PATH_RESPONSE only counts from the address it was sent to."""
        entry = self.challenges.pop(bytes(data), None)
        if entry is None:
            return False
        expected_address, _sent_at = entry
        if tuple(address) != expected_address:
            # Returning the data from elsewhere proves nothing about this path.
            return False
        self.validated_paths.add(tuple(address))
        self.migrations += 1
        return True

    def is_validated(self, address):
        return tuple(address) in self.validated_paths

    def describe(self):
        return {
            "pending_challenges": len(self.challenges),
            "validated_paths": [f"{h}:{p}" for h, p in sorted(self.validated_paths)],
            "migrations": self.migrations,
        }


__all__ = [
    "AMPLIFICATION_LIMIT",
    "AddressValidator",
    "AmplificationLimit",
    "PATH_CHALLENGE_SIZE",
    "PathValidator",
    "TOKEN_LIFETIME_SECONDS",
]
