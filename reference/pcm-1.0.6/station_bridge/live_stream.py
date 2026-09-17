"""Bounded, independently decodable MP3 frames for a live HTTP response.

No audio is written to disk. Every frame is MPEG-1 Layer III, 320 kbps,
48 kHz, stereo, with the bit reservoir disabled by the encoder. A response
has a finite frame budget, so its Content-Length is computable in advance.
Stopping playback cancels the HTTP transfer rather than claiming completion.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import threading
import time

FRAME_BYTES = 960
FRAME_SECONDS = 1152 / 48000  # 24 ms; no padding at this sample rate/bitrate
RESPONSE_FRAMES = 900_000  # at most 6 hours per response, 864,000,000 bytes


class MP3FrameParser:
    """Accumulate arbitrary pipe reads, emit complete independent MP3 frames."""
    def __init__(self):
        self.pending = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.pending.extend(data)
        frames = []
        while len(self.pending) >= FRAME_BYTES:
            h = self.pending
            # sync, MPEG1, layer III, 320k, 48k, no padding
            if h[0] != 0xff or h[1] & 0xfe != 0xfa or h[2] != 0xe4:
                raise ValueError('Unexpected MP3 framing; live stream not published.')
            # main_data_begin == 0: frames never depend on a previous request.
            side = 4 if h[1] & 1 else 6
            if h[side] != 0 or h[side + 1] & 0x80:
                raise ValueError('MP3 bit reservoir must be disabled.')
            frames.append(bytes(h[:FRAME_BYTES]))
            del self.pending[:FRAME_BYTES]
        return frames


@dataclass(frozen=True)
class FrameRead:
    data: bytes
    cursor: int
    skipped: int
    newest: int


class FrameRing:
    """Sequence-addressed ring; slow readers cannot block capture or grow RAM."""
    def __init__(self, capacity: int = 128, *, frame_bytes: int = FRAME_BYTES,
                 frame_seconds: float = FRAME_SECONDS):
        if capacity < 8 or capacity > 2048:
            raise ValueError('Invalid frame ring size')
        if not 1 <= frame_bytes <= 65536 or not 0 < frame_seconds <= 1:
            raise ValueError('Invalid frame dimensions')
        self.frame_bytes, self.frame_seconds = frame_bytes, frame_seconds
        self.frames: deque[bytes] = deque(maxlen=capacity)
        self.total = 0
        self.first_frame_at: float | None = None
        self.last_frame_at: float | None = None
        self.lock = threading.Lock()

    def append(self, frame: bytes):
        if len(frame) != self.frame_bytes:
            raise ValueError('Incomplete MP3 frame')
        with self.lock:
            now = time.monotonic()
            if self.first_frame_at is None:
                self.first_frame_at = now
            self.last_frame_at = now
            self.frames.append(frame)
            self.total += 1

    def live_cursor(self, preroll: int = 6) -> int:
        with self.lock:
            return max(self.total - len(self.frames), self.total - preroll)

    def read(self, cursor: int, count: int = 3) -> FrameRead:
        with self.lock:
            first = self.total - len(self.frames)
            skipped = max(0, first - cursor)
            cursor = max(first, min(cursor, self.total))
            frames = list(self.frames)[cursor - first:cursor - first + count]
            return FrameRead(b''.join(frames), cursor + len(frames), skipped, self.total)

    def snapshot(self) -> dict:
        with self.lock:
            return {'encoded_frames': self.total, 'retained_frames': len(self.frames),
                    'retained_bytes': len(self.frames) * self.frame_bytes,
                    'encoded_seconds': round(self.total * self.frame_seconds, 3),
                    'last_encoded_frame_ago': (round(time.monotonic() - self.last_frame_at, 3)
                                               if self.last_frame_at is not None else None)}
