import unittest

from wsbuilder.quic.address import (
    AMPLIFICATION_LIMIT,
    AddressValidator,
    AmplificationLimit,
    PathValidator,
)


class Clock:
    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now


class TestAddressValidator(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.validator = AddressValidator(time_source=self.clock)
        self.address = ("192.0.2.1", 443)

    def test_a_token_round_trips(self):
        token = self.validator.issue(self.address, b"\x01\x02\x03\x04")
        self.assertEqual(self.validator.validate(token, self.address), b"\x01\x02\x03\x04")

    def test_a_token_is_bound_to_the_address(self):
        # Replaying it from elsewhere proves nothing about that address.
        token = self.validator.issue(self.address, b"\x01\x02")
        self.assertIsNone(self.validator.validate(token, ("198.51.100.9", 443)))

    def test_a_token_is_bound_to_the_port_too(self):
        token = self.validator.issue(self.address, b"\x01\x02")
        self.assertIsNone(self.validator.validate(token, ("192.0.2.1", 444)))

    def test_a_tampered_token_is_refused(self):
        token = bytearray(self.validator.issue(self.address, b"\x01\x02"))
        token[-1] ^= 1
        self.assertIsNone(self.validator.validate(bytes(token), self.address))

    def test_a_token_from_another_server_is_refused(self):
        other = AddressValidator(time_source=self.clock)
        token = other.issue(self.address, b"\x01\x02")
        self.assertIsNone(self.validator.validate(token, self.address))

    def test_an_expired_token_is_refused(self):
        token = self.validator.issue(self.address, b"\x01\x02")
        self.clock.now += self.validator.lifetime + 1
        self.assertIsNone(self.validator.validate(token, self.address))

    def test_rubbish_is_refused_without_raising(self):
        for value in (b"", b"short", None, b"\x00" * 24):
            with self.subTest(token=value):
                self.assertIsNone(self.validator.validate(value, self.address))


class TestAmplificationLimit(unittest.TestCase):
    def test_nothing_may_be_sent_before_anything_arrives(self):
        self.assertEqual(AmplificationLimit().budget(), 0)

    def test_the_budget_is_three_times_what_arrived(self):
        limit = AmplificationLimit()
        limit.on_received(1200)
        self.assertEqual(limit.budget(), 1200 * AMPLIFICATION_LIMIT)
        self.assertTrue(limit.may_send(3600))
        self.assertFalse(limit.may_send(3601))

    def test_sending_spends_the_budget(self):
        limit = AmplificationLimit()
        limit.on_received(1200)
        limit.on_sent(3000)
        self.assertEqual(limit.budget(), 600)

    def test_validation_removes_the_cap(self):
        limit = AmplificationLimit()
        limit.on_received(10)
        limit.validate()
        self.assertIsNone(limit.budget())
        self.assertTrue(limit.may_send(1_000_000))


class TestPathValidator(unittest.TestCase):
    def setUp(self):
        self.validator = PathValidator()
        self.address = ("203.0.113.5", 51000)

    def test_a_matching_response_validates_the_path(self):
        data = self.validator.challenge(self.address)
        self.assertTrue(self.validator.on_response(data, self.address))
        self.assertTrue(self.validator.is_validated(self.address))
        self.assertEqual(self.validator.migrations, 1)

    def test_a_response_from_elsewhere_proves_nothing(self):
        data = self.validator.challenge(self.address)
        self.assertFalse(self.validator.on_response(data, ("198.51.100.1", 1)))
        self.assertFalse(self.validator.is_validated(self.address))

    def test_unknown_data_is_refused(self):
        self.validator.challenge(self.address)
        self.assertFalse(self.validator.on_response(b"12345678", self.address))

    def test_a_challenge_is_answered_once(self):
        data = self.validator.challenge(self.address)
        self.assertTrue(self.validator.on_response(data, self.address))
        # Replaying it must not count as a second validation.
        self.assertFalse(self.validator.on_response(data, self.address))

    def test_challenges_are_unpredictable(self):
        seen = {self.validator.challenge(self.address) for _ in range(20)}
        self.assertEqual(len(seen), 20)


if __name__ == "__main__":
    unittest.main()
