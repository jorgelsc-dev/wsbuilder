"""Loss detection and congestion control for QUIC (RFC 9002).

Acknowledgements alone are not delivery. A sender has to notice what never
arrived and send it again, and it has to slow down when the path says so.
Those are two jobs with one input -- the arrival of an ACK -- and RFC 9002
keeps them separate: loss detection decides *what* to resend, congestion
control decides *how much* may be in flight.

Three details are easy to get wrong and are worth naming:

* Only ack-eliciting packets arm a timer. A packet carrying nothing but an
  ACK is never retransmitted, because the peer will tell us again anyway.
* Packet number spaces are independent. An Initial packet is never declared
  lost because a 1-RTT packet was acknowledged past it.
* A packet is lost by *order* as well as by time. Three later packets
  acknowledged is stronger evidence than a clock, and much faster.
"""

import time

#: RFC 9002 section 6.1.1: how many later packets must be acknowledged.
PACKET_THRESHOLD = 3
#: Section 6.1.2, as a fraction of the larger of smoothed and latest RTT.
TIME_THRESHOLD = 9 / 8
#: Section 6.1.2: the smallest delay worth distinguishing.
GRANULARITY = 0.001
#: Section 6.2.2, used until the first RTT sample.
INITIAL_RTT = 0.333

MAX_DATAGRAM_SIZE = 1200
#: Section 7.2.
INITIAL_WINDOW = min(10 * MAX_DATAGRAM_SIZE, max(14720, 2 * MAX_DATAGRAM_SIZE))
MINIMUM_WINDOW = 2 * MAX_DATAGRAM_SIZE
LOSS_REDUCTION_FACTOR = 0.5
PERSISTENT_CONGESTION_THRESHOLD = 3


class SentPacket:
    """One packet we sent, kept until it is acknowledged or declared lost."""

    __slots__ = (
        "packet_number", "time_sent", "ack_eliciting", "in_flight", "size", "frames", "level",
    )

    def __init__(self, packet_number, time_sent, *, frames=(), ack_eliciting=True,
                 in_flight=True, size=0, level=""):
        self.packet_number = int(packet_number)
        self.time_sent = float(time_sent)
        self.ack_eliciting = bool(ack_eliciting)
        self.in_flight = bool(in_flight)
        self.size = int(size)
        self.frames = list(frames)
        #: Packet number space, so a probe goes back out at the right level.
        self.level = str(level)

    def __repr__(self):
        return f"<SentPacket {self.packet_number} size={self.size}>"


class RttEstimator:
    """Smoothed round-trip time and its variation (RFC 9002 section 5)."""

    def __init__(self, initial_rtt=INITIAL_RTT):
        self.latest_rtt = 0.0
        self.min_rtt = 0.0
        self.smoothed_rtt = float(initial_rtt)
        self.rttvar = float(initial_rtt) / 2
        self.samples = 0

    def update(self, latest_rtt, ack_delay=0.0, max_ack_delay=0.025):
        latest_rtt = float(latest_rtt)
        if latest_rtt <= 0:
            return
        self.latest_rtt = latest_rtt
        if self.samples == 0:
            self.min_rtt = latest_rtt
            self.smoothed_rtt = latest_rtt
            self.rttvar = latest_rtt / 2
            self.samples = 1
            return

        self.min_rtt = min(self.min_rtt, latest_rtt)
        # Section 5.3: a peer's reported delay is only trustworthy as far as
        # it still leaves the sample above the minimum seen on this path.
        adjusted = latest_rtt
        delay = min(float(ack_delay), float(max_ack_delay))
        if latest_rtt >= self.min_rtt + delay:
            adjusted = latest_rtt - delay

        self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.smoothed_rtt - adjusted)
        self.smoothed_rtt = 0.875 * self.smoothed_rtt + 0.125 * adjusted
        self.samples += 1

    def loss_delay(self):
        """How long to wait before time alone counts as loss."""
        if self.samples == 0:
            return max(TIME_THRESHOLD * INITIAL_RTT, GRANULARITY)
        return max(TIME_THRESHOLD * max(self.smoothed_rtt, self.latest_rtt), GRANULARITY)

    def pto(self, max_ack_delay=0.025):
        """Probe timeout: long enough that a quiet path means loss."""
        return self.smoothed_rtt + max(4 * self.rttvar, GRANULARITY) + float(max_ack_delay)

    def describe(self):
        return {
            "latest_rtt_ms": round(self.latest_rtt * 1000, 3),
            "smoothed_rtt_ms": round(self.smoothed_rtt * 1000, 3),
            "rttvar_ms": round(self.rttvar * 1000, 3),
            "min_rtt_ms": round(self.min_rtt * 1000, 3),
            "samples": self.samples,
        }


class CongestionControl:
    """NewReno, the algorithm RFC 9002 section 7 specifies by default."""

    def __init__(self, max_datagram_size=MAX_DATAGRAM_SIZE):
        self.max_datagram_size = int(max_datagram_size)
        self.congestion_window = INITIAL_WINDOW
        self.bytes_in_flight = 0
        self.ssthresh = None
        self.recovery_start_time = 0.0
        self.congestion_events = 0

    @property
    def in_slow_start(self):
        return self.ssthresh is None or self.congestion_window < self.ssthresh

    def can_send(self, size):
        return self.bytes_in_flight + int(size) <= self.congestion_window

    def available(self):
        return max(0, self.congestion_window - self.bytes_in_flight)

    def on_packet_sent(self, size):
        self.bytes_in_flight += int(size)

    def on_packets_acked(self, packets, now):
        for packet in packets:
            if not packet.in_flight:
                continue
            self.bytes_in_flight = max(0, self.bytes_in_flight - packet.size)
            if packet.time_sent <= self.recovery_start_time:
                # Sent before we reacted; growing on it would undo the cut.
                continue
            if self.in_slow_start:
                self.congestion_window += packet.size
            else:
                self.congestion_window += (
                    self.max_datagram_size * packet.size // max(1, self.congestion_window)
                )

    def on_packets_lost(self, packets, now):
        largest_sent = 0.0
        for packet in packets:
            if packet.in_flight:
                self.bytes_in_flight = max(0, self.bytes_in_flight - packet.size)
            largest_sent = max(largest_sent, packet.time_sent)
        if largest_sent:
            self._enter_recovery(largest_sent, now)

    def _enter_recovery(self, sent_time, now):
        if sent_time <= self.recovery_start_time:
            # One congestion event per round trip, not one per lost packet.
            return
        self.recovery_start_time = now
        self.congestion_window = max(
            int(self.congestion_window * LOSS_REDUCTION_FACTOR), MINIMUM_WINDOW
        )
        self.ssthresh = self.congestion_window
        self.congestion_events += 1

    def describe(self):
        return {
            "congestion_window": self.congestion_window,
            "bytes_in_flight": self.bytes_in_flight,
            "ssthresh": self.ssthresh,
            "slow_start": self.in_slow_start,
            "congestion_events": self.congestion_events,
        }


class LossRecovery:
    """Tracks what is in flight per packet number space and what was lost."""

    def __init__(self, levels=("initial", "handshake", "application"),
                 max_ack_delay=0.025, time_source=None):
        self.levels = tuple(levels)
        self.max_ack_delay = float(max_ack_delay)
        self.rtt = RttEstimator()
        self.congestion = CongestionControl()
        self._now = time_source or time.monotonic
        self.sent = {level: {} for level in self.levels}
        self.largest_acked = {level: None for level in self.levels}
        self.loss_time = {level: None for level in self.levels}
        self.time_of_last_ack_eliciting = {level: None for level in self.levels}
        self.pto_count = 0
        self.lost_total = 0

    # -- sending -------------------------------------------------------

    def on_packet_sent(self, level, packet):
        self.sent[level][packet.packet_number] = packet
        if packet.in_flight:
            self.congestion.on_packet_sent(packet.size)
        if packet.ack_eliciting:
            self.time_of_last_ack_eliciting[level] = packet.time_sent

    # -- receiving -----------------------------------------------------

    @staticmethod
    def acknowledged_numbers(largest, ranges):
        """Expand an ACK frame's ranges back into packet numbers."""
        if largest is None:
            return set()
        numbers = set()
        first = ranges[0] if ranges else 0
        smallest = largest - first
        numbers.update(range(smallest, largest + 1))
        for gap, length in (ranges[1:] if ranges else []):
            largest = smallest - gap - 2
            smallest = largest - length
            if largest < 0:
                break
            numbers.update(range(max(0, smallest), largest + 1))
        return numbers

    def on_ack_received(self, level, largest, ranges, ack_delay=0.0, now=None):
        """Apply an ACK. Returns ``(newly_acked, lost)``."""
        now = self._now() if now is None else now
        acked_numbers = self.acknowledged_numbers(largest, ranges)
        tracked = self.sent[level]
        newly_acked = [tracked.pop(number) for number in sorted(acked_numbers) if number in tracked]
        if not newly_acked:
            return [], self.detect_lost_packets(level, now=now)

        previous = self.largest_acked[level]
        self.largest_acked[level] = largest if previous is None else max(previous, largest)

        newest = max(newly_acked, key=lambda packet: packet.packet_number)
        if newest.packet_number == largest and newest.ack_eliciting:
            # Only the largest acknowledged packet gives a usable sample.
            self.rtt.update(now - newest.time_sent, ack_delay, self.max_ack_delay)

        self.congestion.on_packets_acked(newly_acked, now)
        self.pto_count = 0
        return newly_acked, self.detect_lost_packets(level, now=now)

    def detect_lost_packets(self, level, now=None):
        """Declare packets lost by order or by time (RFC 9002 section 6.1)."""
        now = self._now() if now is None else now
        largest = self.largest_acked[level]
        if largest is None:
            return []
        threshold = now - self.rtt.loss_delay()
        tracked = self.sent[level]
        lost = []
        earliest_pending = None
        for number, packet in sorted(tracked.items()):
            if number > largest:
                continue
            if number <= largest - PACKET_THRESHOLD or packet.time_sent <= threshold:
                lost.append(packet)
            else:
                lost_at = packet.time_sent + self.rtt.loss_delay()
                earliest_pending = (
                    lost_at if earliest_pending is None else min(earliest_pending, lost_at)
                )
        for packet in lost:
            tracked.pop(packet.packet_number, None)
        self.loss_time[level] = earliest_pending
        if lost:
            self.lost_total += len(lost)
            self.congestion.on_packets_lost(lost, now)
        return lost

    # -- timers --------------------------------------------------------

    def loss_detection_timer(self):
        """When to wake: the earlier of a pending loss and the probe timeout."""
        times = [value for value in self.loss_time.values() if value is not None]
        if times:
            return min(times)
        pending = [
            value for value in self.time_of_last_ack_eliciting.values() if value is not None
        ]
        if not pending:
            return None
        timeout = self.rtt.pto(self.max_ack_delay) * (2 ** self.pto_count)
        return max(pending) + timeout

    def on_timeout(self, now=None):
        """Handle an expired timer. Returns the packets needing a probe."""
        now = self._now() if now is None else now
        lost = []
        for level in self.levels:
            pending = self.loss_time[level]
            if pending is not None and pending <= now:
                lost.extend(self.detect_lost_packets(level, now=now))
        if lost:
            return lost

        # Nothing timed out by loss: this is a probe timeout, so resend the
        # oldest ack-eliciting packet rather than wait for silence to end.
        self.pto_count += 1
        probes = []
        for level in self.levels:
            tracked = self.sent[level]
            eliciting = [p for p in tracked.values() if p.ack_eliciting]
            if eliciting:
                probes.append(min(eliciting, key=lambda packet: packet.packet_number))
        return probes

    def bytes_in_flight(self):
        return self.congestion.bytes_in_flight

    def describe(self):
        return {
            "rtt": self.rtt.describe(),
            "congestion": self.congestion.describe(),
            "pto_count": self.pto_count,
            "lost_total": self.lost_total,
            "in_flight": {level: len(self.sent[level]) for level in self.levels},
        }


__all__ = [
    "CongestionControl",
    "GRANULARITY",
    "INITIAL_RTT",
    "INITIAL_WINDOW",
    "LossRecovery",
    "MINIMUM_WINDOW",
    "PACKET_THRESHOLD",
    "RttEstimator",
    "SentPacket",
    "TIME_THRESHOLD",
]
