import unittest

from wsbuilder.quic.recovery import (
    GRANULARITY,
    INITIAL_WINDOW,
    MINIMUM_WINDOW,
    PACKET_THRESHOLD,
    CongestionControl,
    LossRecovery,
    RttEstimator,
    SentPacket,
)


class Clock:
    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


class TestAckRangeExpansion(unittest.TestCase):
    """The inverse of what an ACK frame encodes."""

    def test_a_single_range(self):
        self.assertEqual(LossRecovery.acknowledged_numbers(9, [3]), {6, 7, 8, 9})

    def test_one_packet(self):
        self.assertEqual(LossRecovery.acknowledged_numbers(0, [0]), {0})

    def test_a_gap_between_ranges(self):
        # largest 9, four below it, then two unacked, then three more.
        self.assertEqual(
            LossRecovery.acknowledged_numbers(9, [3, (2, 2)]), {0, 1, 2, 6, 7, 8, 9}
        )

    def test_nothing_acknowledged(self):
        self.assertEqual(LossRecovery.acknowledged_numbers(None, []), set())

    def test_it_inverts_what_the_connection_encodes(self):
        from wsbuilder.quic.connection import QuicConnection

        for received in ([0], [0, 1, 2], [0, 1, 3, 4], [1, 3, 5], list(range(12))):
            with self.subTest(received=received):
                largest, ranges = QuicConnection.ack_ranges(received)
                self.assertEqual(
                    LossRecovery.acknowledged_numbers(largest, ranges), set(received)
                )


class TestRttEstimation(unittest.TestCase):
    def test_the_first_sample_sets_everything(self):
        rtt = RttEstimator()
        rtt.update(0.100)
        self.assertEqual(rtt.smoothed_rtt, 0.100)
        self.assertEqual(rtt.min_rtt, 0.100)
        self.assertEqual(rtt.rttvar, 0.050)

    def test_later_samples_are_smoothed_not_replaced(self):
        rtt = RttEstimator()
        rtt.update(0.100)
        rtt.update(0.200)
        self.assertGreater(rtt.smoothed_rtt, 0.100)
        self.assertLess(rtt.smoothed_rtt, 0.200)

    def test_the_minimum_is_remembered(self):
        rtt = RttEstimator()
        rtt.update(0.100)
        rtt.update(0.050)
        rtt.update(0.300)
        self.assertEqual(rtt.min_rtt, 0.050)

    def test_a_reported_delay_is_only_removed_when_it_still_fits(self):
        rtt = RttEstimator()
        rtt.update(0.100)
        before = rtt.smoothed_rtt
        # A delay large enough to push the sample under min_rtt is ignored.
        rtt.update(0.101, ack_delay=1.0, max_ack_delay=1.0)
        self.assertGreater(rtt.smoothed_rtt, before * 0.5)

    def test_a_non_positive_sample_is_discarded(self):
        rtt = RttEstimator()
        rtt.update(0.100)
        rtt.update(-1)
        self.assertEqual(rtt.samples, 1)

    def test_the_loss_delay_never_goes_below_the_granularity(self):
        rtt = RttEstimator()
        rtt.update(0.0000001)
        self.assertGreaterEqual(rtt.loss_delay(), GRANULARITY)


class TestLossDetection(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.recovery = LossRecovery(time_source=self.clock)

    def _send(self, count, level="application"):
        for number in range(count):
            self.clock.advance(0.001)
            self.recovery.on_packet_sent(
                level,
                SentPacket(number, self.clock.now, size=1200, level=level),
            )

    def test_packets_far_enough_behind_the_largest_are_lost(self):
        self._send(10)
        self.clock.advance(0.050)
        acked, lost = self.recovery.on_ack_received(
            "application", 9, [3, (2, 2)], now=self.clock.now
        )
        self.assertEqual(sorted(p.packet_number for p in acked), [0, 1, 2, 6, 7, 8, 9])
        self.assertEqual(sorted(p.packet_number for p in lost), [3, 4, 5])

    def test_the_threshold_is_three_packets(self):
        self._send(5)
        # A realistic round trip, so the time rule cannot fire first: an
        # unrealistically short one makes every packet look ancient.
        self.clock.advance(0.050)
        _acked, lost = self.recovery.on_ack_received(
            "application", PACKET_THRESHOLD, [0], now=self.clock.now
        )
        # Packet 0 is three behind the acknowledged 3, so only it is lost.
        self.assertEqual([p.packet_number for p in lost], [0])

    def test_a_packet_still_recent_is_not_declared_lost(self):
        self._send(3)
        self.clock.advance(0.050)
        _acked, lost = self.recovery.on_ack_received(
            "application", 2, [0], now=self.clock.now
        )
        self.assertEqual(lost, [])

    def test_time_alone_declares_loss_without_three_later_packets(self):
        # Two packets, then a long pause, then one more acknowledged quickly.
        # The RTT sample stays small, so the first two are old by comparison
        # even though only one packet follows them.
        self._send(2)
        self.clock.advance(1.0)
        self.recovery.on_packet_sent(
            "application", SentPacket(2, self.clock.now, size=1200, level="application")
        )
        self.clock.advance(0.050)
        _acked, lost = self.recovery.on_ack_received(
            "application", 2, [0], now=self.clock.now
        )
        self.assertEqual(sorted(p.packet_number for p in lost), [0, 1])

    def test_packet_number_spaces_are_independent(self):
        self._send(3, level="initial")
        self._send(3, level="application")
        self.clock.advance(0.050)
        self.recovery.on_ack_received("application", 2, [2], now=self.clock.now)
        # Acknowledging 1-RTT packets says nothing about Initial ones.
        self.assertEqual(len(self.recovery.sent["initial"]), 3)

    def test_an_rtt_sample_comes_only_from_the_largest(self):
        self._send(5)
        self.clock.advance(0.080)
        self.recovery.on_ack_received("application", 4, [4], now=self.clock.now)
        self.assertGreater(self.recovery.rtt.smoothed_rtt, 0.07)
        self.assertLess(self.recovery.rtt.smoothed_rtt, 0.09)


class TestProbeTimeout(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(2000.0)
        self.recovery = LossRecovery(time_source=self.clock)

    def test_a_timer_is_armed_while_something_is_in_flight(self):
        self.recovery.on_packet_sent(
            "application", SentPacket(0, self.clock.now, size=1200, level="application")
        )
        self.assertIsNotNone(self.recovery.loss_detection_timer())

    def test_nothing_in_flight_arms_nothing(self):
        self.assertIsNone(self.recovery.loss_detection_timer())

    def test_a_probe_resends_the_oldest_unacknowledged_packet(self):
        for number in range(3):
            self.clock.advance(0.001)
            self.recovery.on_packet_sent(
                "application",
                SentPacket(number, self.clock.now, size=1200, level="application"),
            )
        self.clock.advance(10.0)
        probes = self.recovery.on_timeout(now=self.clock.now)
        self.assertEqual([p.packet_number for p in probes], [0])
        self.assertEqual(self.recovery.pto_count, 1)

    def test_repeated_timeouts_back_off(self):
        self.recovery.on_packet_sent(
            "application", SentPacket(0, self.clock.now, size=1200, level="application")
        )
        first = self.recovery.loss_detection_timer()
        self.clock.advance(10.0)
        self.recovery.on_timeout(now=self.clock.now)
        second = self.recovery.loss_detection_timer()
        self.assertEqual(self.recovery.pto_count, 1)
        # The timer is armed from the same send, so a later deadline is the
        # backoff and nothing else.
        self.assertGreater(second, first)

    def test_an_acknowledgement_resets_the_backoff(self):
        self.recovery.on_packet_sent(
            "application", SentPacket(0, self.clock.now, size=1200, level="application")
        )
        self.clock.advance(10.0)
        self.recovery.on_timeout(now=self.clock.now)
        self.assertEqual(self.recovery.pto_count, 1)
        self.recovery.on_ack_received("application", 0, [0], now=self.clock.now)
        self.assertEqual(self.recovery.pto_count, 0)

    def test_an_ack_only_packet_never_arms_a_probe(self):
        # It would probe for ever: the peer owes no acknowledgement for it.
        self.recovery.on_packet_sent(
            "application",
            SentPacket(0, self.clock.now, size=60, ack_eliciting=False,
                       in_flight=False, level="application"),
        )
        self.assertIsNone(self.recovery.loss_detection_timer())


class TestCongestionControl(unittest.TestCase):
    def test_it_starts_in_slow_start(self):
        control = CongestionControl()
        self.assertEqual(control.congestion_window, INITIAL_WINDOW)
        self.assertTrue(control.in_slow_start)

    def test_slow_start_grows_by_what_was_acknowledged(self):
        control = CongestionControl()
        before = control.congestion_window
        packet = SentPacket(0, 1000.0, size=1200)
        control.on_packet_sent(1200)
        control.on_packets_acked([packet], 1100.0)
        self.assertEqual(control.congestion_window, before + 1200)

    def test_loss_halves_the_window_and_ends_slow_start(self):
        control = CongestionControl()
        control.on_packet_sent(1200)
        control.on_packets_lost([SentPacket(0, 1050.0, size=1200)], 1100.0)
        self.assertEqual(control.congestion_window, INITIAL_WINDOW // 2)
        self.assertFalse(control.in_slow_start)

    def test_the_window_never_falls_below_the_floor(self):
        control = CongestionControl()
        for round_trip in range(10):
            control.on_packets_lost(
                [SentPacket(round_trip, 1000.0 + round_trip * 10, size=1200)],
                1000.0 + round_trip * 10 + 1,
            )
        self.assertGreaterEqual(control.congestion_window, MINIMUM_WINDOW)

    def test_one_congestion_event_per_round_trip(self):
        control = CongestionControl()
        # Packets sent before the reaction must not cut the window again.
        control.on_packets_lost([SentPacket(0, 1000.0, size=1200)], 1100.0)
        control.on_packets_lost([SentPacket(1, 1050.0, size=1200)], 1101.0)
        self.assertEqual(control.congestion_events, 1)

    def test_bytes_in_flight_falls_as_packets_leave(self):
        control = CongestionControl()
        control.on_packet_sent(1200)
        control.on_packet_sent(1200)
        self.assertEqual(control.bytes_in_flight, 2400)
        control.on_packets_acked([SentPacket(0, 1000.0, size=1200)], 1100.0)
        self.assertEqual(control.bytes_in_flight, 1200)

    def test_sending_is_refused_past_the_window(self):
        control = CongestionControl()
        control.on_packet_sent(control.congestion_window)
        self.assertFalse(control.can_send(1))
        self.assertEqual(control.available(), 0)

    def test_congestion_avoidance_grows_slower_than_slow_start(self):
        control = CongestionControl()
        control.on_packets_lost([SentPacket(0, 1000.0, size=1200)], 1100.0)
        before = control.congestion_window
        control.on_packet_sent(1200)
        control.on_packets_acked([SentPacket(1, 1200.0, size=1200)], 1300.0)
        self.assertLess(control.congestion_window - before, 1200)


if __name__ == "__main__":
    unittest.main()
