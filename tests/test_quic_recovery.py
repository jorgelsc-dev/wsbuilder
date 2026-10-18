import threading
import unittest

from wsbuilder.quic import frames as qf
from wsbuilder.quic.connection import (
    LEVEL_APPLICATION,
    STREAM_CHUNK_SIZE,
    TP_INITIAL_MAX_DATA,
    TP_INITIAL_MAX_STREAM_DATA_BIDI_LOCAL,
    TP_INITIAL_MAX_STREAM_DATA_UNI,
    QuicConnection,
)
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


def _sending_connection(parameters=None):
    """A connection with only the state the send path touches.

    ``_flush`` is replaced by a recorder, so the test sees the frames that
    would have gone out and nothing needs keys or a socket.
    """
    connection = QuicConnection.__new__(QuicConnection)
    connection.closed = False
    connection.keys = {LEVEL_APPLICATION: {"send": object()}}
    connection._outbound_lock = threading.Lock()
    connection._outbound_room = threading.Condition(connection._outbound_lock)
    connection._outbound = []
    connection._queued_bytes = {}
    connection._send_offsets = {}
    connection._peer_stream_max = {}
    connection._sent_data_total = 0
    connection._blocked_reported = {}
    connection.recovery = LossRecovery(levels=(LEVEL_APPLICATION,), time_source=Clock())
    connection.peer_transport_parameters = {
        key: qf.encode_varint(value) for key, value in (parameters or {}).items()
    }
    connection._peer_max_data = connection._peer_parameter(TP_INITIAL_MAX_DATA)
    connection.sent_frames = []

    def record(outgoing):
        datagrams = []
        for _level, frame_list in outgoing:
            connection.sent_frames.extend(frame_list)
            size = sum(len(frame.serialize()) for frame in frame_list) + 40
            connection.recovery.congestion.on_packet_sent(size)
            datagrams.append(b"d" * size)
        return datagrams

    connection._flush = record
    return connection


def _stream_bytes(connection, stream_id):
    return sum(
        len(frame.data)
        for frame in connection.sent_frames
        if isinstance(frame, qf.StreamFrame) and frame.stream_id == stream_id
    )


class TestSendFlowControl(unittest.TestCase):
    def test_stream_limit_from_transport_parameters_is_respected(self):
        connection = _sending_connection(
            {TP_INITIAL_MAX_DATA: 1 << 20, TP_INITIAL_MAX_STREAM_DATA_BIDI_LOCAL: 1500}
        )
        connection.queue_stream_data(0, b"x" * 4000, fin=True)
        connection.drain_outbound()

        self.assertEqual(_stream_bytes(connection, 0), 1500)
        (blocked,) = [f for f in connection.sent_frames if isinstance(f, qf.StreamDataBlockedFrame)]
        self.assertEqual((blocked.stream_id, blocked.maximum), (0, 1500))
        self.assertEqual(connection.queued_stream_bytes(0), 2500)
        self.assertFalse(any(getattr(f, "fin", False) for f in connection.sent_frames))

        # MAX_STREAM_DATA from the peer releases the rest, FIN included.
        connection._handle_frames(LEVEL_APPLICATION, [qf.MaxStreamDataFrame(0, 10_000)], [])
        connection.drain_outbound()
        self.assertEqual(_stream_bytes(connection, 0), 4000)
        self.assertTrue(connection.sent_frames[-1].fin)
        self.assertEqual(connection.queued_stream_bytes(0), 0)

    def test_connection_limit_and_max_data(self):
        connection = _sending_connection(
            {
                TP_INITIAL_MAX_DATA: 1200,
                TP_INITIAL_MAX_STREAM_DATA_BIDI_LOCAL: 1 << 20,
                TP_INITIAL_MAX_STREAM_DATA_UNI: 1 << 20,
            }
        )
        connection.queue_stream_data(0, b"a" * 1000)
        connection.queue_stream_data(4, b"b" * 1000)
        connection.drain_outbound()

        self.assertEqual(_stream_bytes(connection, 0) + _stream_bytes(connection, 4), 1200)
        blocked = [f for f in connection.sent_frames if isinstance(f, qf.DataBlockedFrame)]
        self.assertEqual([f.maximum for f in blocked], [1200])
        # Reported once per limit, not on every drain.
        connection.drain_outbound()
        blocked = [f for f in connection.sent_frames if isinstance(f, qf.DataBlockedFrame)]
        self.assertEqual(len(blocked), 1)

        connection._handle_frames(LEVEL_APPLICATION, [qf.MaxDataFrame(5000)], [])
        connection.drain_outbound()
        self.assertEqual(_stream_bytes(connection, 0), 1000)
        self.assertEqual(_stream_bytes(connection, 4), 1000)

    def test_a_smaller_max_data_never_lowers_the_limit(self):
        connection = _sending_connection({TP_INITIAL_MAX_DATA: 3000})
        connection._handle_frames(LEVEL_APPLICATION, [qf.MaxDataFrame(100)], [])
        self.assertEqual(connection._peer_max_data, 3000)

    def test_a_blocked_stream_does_not_hold_up_another(self):
        connection = _sending_connection(
            {
                TP_INITIAL_MAX_DATA: 1 << 20,
                TP_INITIAL_MAX_STREAM_DATA_BIDI_LOCAL: 10,
                TP_INITIAL_MAX_STREAM_DATA_UNI: 1 << 20,
            }
        )
        connection.queue_stream_data(0, b"a" * 100)
        connection.queue_stream_data(3, b"c" * 100)
        connection.drain_outbound()
        self.assertEqual(_stream_bytes(connection, 0), 10)
        self.assertEqual(_stream_bytes(connection, 3), 100)

    def test_missing_parameters_mean_zero_but_no_parameters_mean_unlimited(self):
        limited = _sending_connection({TP_INITIAL_MAX_DATA: 1 << 20})
        limited.queue_stream_data(0, b"x" * 50)
        limited.drain_outbound()
        self.assertEqual(_stream_bytes(limited, 0), 0)

        legacy = _sending_connection()
        legacy.queue_stream_data(0, b"x" * 50)
        legacy.drain_outbound()
        self.assertEqual(_stream_bytes(legacy, 0), 50)


class TestSendCongestionControl(unittest.TestCase):
    def test_the_congestion_window_holds_back_new_data(self):
        connection = _sending_connection()
        connection.queue_stream_data(0, b"x" * (INITIAL_WINDOW * 3))
        connection.drain_outbound()
        congestion = connection.recovery.congestion

        sent = _stream_bytes(connection, 0)
        self.assertGreater(sent, 0)
        self.assertLess(sent, INITIAL_WINDOW)
        self.assertLessEqual(congestion.bytes_in_flight, congestion.congestion_window)
        self.assertEqual(connection.queued_stream_bytes(0), INITIAL_WINDOW * 3 - sent)

        # Nothing more leaves until acknowledgements free the window.
        connection.drain_outbound()
        self.assertEqual(_stream_bytes(connection, 0), sent)
        congestion.bytes_in_flight = 0
        connection.drain_outbound()
        self.assertGreater(_stream_bytes(connection, 0), sent)

    def test_stream_order_is_kept_across_blocked_drains(self):
        connection = _sending_connection()
        payload = bytes(range(256)) * 200
        connection.queue_stream_data(0, payload, fin=True)
        for _ in range(20):
            connection.drain_outbound()
            connection.recovery.congestion.bytes_in_flight = 0
        frames = [f for f in connection.sent_frames if isinstance(f, qf.StreamFrame)]
        offsets = [f.offset for f in frames]
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual(b"".join(f.data for f in frames), payload)
        self.assertTrue(frames[-1].fin)
        self.assertEqual(len(frames), -(-len(payload) // STREAM_CHUNK_SIZE))


class TestSendBackpressure(unittest.TestCase):
    def test_wait_for_send_room_returns_once_bytes_leave(self):
        connection = _sending_connection({TP_INITIAL_MAX_DATA: 0})
        connection.queue_stream_data(0, b"x" * 5000)
        self.assertFalse(connection.wait_for_send_room(0, 1000, timeout=0.05, poll=0.01))

        released = threading.Event()

        def waiter():
            if connection.wait_for_send_room(0, 1000, timeout=5, poll=0.01):
                released.set()

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        # The peer grants credit: the queued bytes can now leave.
        connection._handle_frames(LEVEL_APPLICATION, [qf.MaxStreamDataFrame(0, 1 << 20)], [])
        connection._handle_frames(LEVEL_APPLICATION, [qf.MaxDataFrame(1 << 20)], [])
        connection.drain_outbound()
        thread.join(timeout=5)
        self.assertTrue(released.is_set())

    def test_wait_for_send_room_gives_up_when_the_connection_closes(self):
        connection = _sending_connection({TP_INITIAL_MAX_DATA: 0})
        connection.queue_stream_data(0, b"x" * 5000)
        connection.closed = True
        self.assertFalse(connection.wait_for_send_room(0, 1000, timeout=5, poll=0.01))

if __name__ == "__main__":
    unittest.main()
