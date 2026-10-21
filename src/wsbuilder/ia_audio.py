"""Bounded PCM WAV decoding and numeric mono audio features (no speech model)."""

import math
import os
import wave
from dataclasses import dataclass

from .ia_text import _positive_integer

__all__ = ["AudioData", "load_wav", "audio_features"]


@dataclass(frozen=True)
class AudioData:
    """Mono samples in [-1, 1] and a positive sample rate in Hz.

    Direct construction accepts a tuple only, so it never consumes an unbounded
    iterable. ``load_wav`` supplies bounded decoded samples.
    """

    samples: tuple[float, ...]
    sample_rate: int

    def __post_init__(self):
        _positive_integer(self.sample_rate, "sample_rate")
        if not isinstance(self.samples, tuple):
            raise TypeError("samples must be a tuple")
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or not -1 <= value <= 1
               for value in self.samples):
            raise ValueError("samples must contain finite numbers in [-1, 1]")

    @property
    def duration(self):
        return len(self.samples) / self.sample_rate


def load_wav(source, *, max_frames=1_000_000, max_channels=8):
    """Read integer PCM WAV (8/16/24/32 bit), averaging channels to mono.

    Opposite-phase channels can cancel during averaging. Source is a path or
    binary seekable file; caller-owned files remain open. Declared frame count
    and channels are checked before sample allocation. Malformed/truncated PCM
    is rejected. Compressed audio and float WAV are not supported.
    """
    _positive_integer(max_frames, "max_frames")
    _positive_integer(max_channels, "max_channels")
    if isinstance(source, os.PathLike):
        source = os.fspath(source)
    with wave.open(source, "rb") as stream:
        channels = stream.getnchannels()
        width = stream.getsampwidth()
        rate = stream.getframerate()
        frames = stream.getnframes()
        if not 1 <= channels <= max_channels:
            raise ValueError("WAV channel count exceeds max_channels")
        if frames > max_frames:
            raise ValueError("WAV frame count exceeds max_frames")
        if stream.getcomptype() != "NONE" or width not in (1, 2, 3, 4):
            raise ValueError("WAV must use 8/16/24/32-bit integer PCM")
        raw = stream.readframes(frames)
        frame_size = width * channels
        if len(raw) != frames * frame_size or stream.readframes(1):
            raise ValueError("WAV contains truncated or incomplete PCM frames")
    samples = []
    scale = 1 << (width * 8 - 1)
    for offset in range(0, len(raw), frame_size):
        total = 0
        for channel in range(channels):
            start = offset + channel * width
            value = int.from_bytes(raw[start:start + width], "little", signed=width != 1)
            total += value - 128 if width == 1 else value
        samples.append(total / (scale * channels))
    return AudioData(tuple(samples), rate)


def audio_features(audio):
    """Return [duration seconds, mean, RMS, peak, zero-crossing fraction].

    Zero crossings compare adjacent signs (zero is nonnegative). Empty audio
    returns five zeros. These summaries discard temporal/spectral information;
    they are suitable for basic signal classification, not transcription.
    """
    if not isinstance(audio, AudioData):
        raise TypeError("audio must be AudioData")
    samples = audio.samples
    if not samples:
        return [0.0] * 5
    crossings = sum((samples[i - 1] < 0) != (samples[i] < 0)
                    for i in range(1, len(samples)))
    return [audio.duration, math.fsum(samples) / len(samples),
            math.sqrt(math.fsum(value * value for value in samples) / len(samples)),
            max(abs(value) for value in samples), crossings / max(1, len(samples) - 1)]
