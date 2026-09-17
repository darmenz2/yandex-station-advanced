"""Receive-driven PCM writer for Advanced and explicit experimental capture.

No real-data pacing sleep, encoder, resampler, or deliberate startup prebuffer.
The callback only records timing and enqueues bytes. A separate writer publishes
complete PCM units immediately, then updates signal meters. A bounded timeout
produces quiet keepalive audio when WASAPI stops issuing callbacks in silence.
There is no busy loop, realtime process priority, or automatic mode switching.
"""
from __future__ import annotations
import array
from collections import deque
import math
import os
import queue
import time
import threading


class ReceiveTiming:
    """Single callback producer, bounded snapshots (no audio payload is retained)."""
    def __init__(self, rate: int):
        self.rate = rate
        self._lock = threading.Lock()
        self.intervals = deque(maxlen=512)
        self.durations = deque(maxlen=512)
        self.last_at = None
        self.count = 0
        self.status_flags = 0
        self.last_frames = 0
        self.max_interval_ms = 0.0
        self.dispatches = 0
        self.last_dispatch_ms = 0.0
        self.max_dispatch_ms = 0.0
        self.dispatch_ms = deque(maxlen=512)
        self.idle_packets = 0
        self.idle_clock_skipped_seconds = 0.0

    def receive(self, now: float, frames: int, status: int = 0) -> None:
        with self._lock:
            if self.last_at is not None:
                interval = max(0.0, (now - self.last_at) * 1000)
                self.intervals.append(interval)
                self.max_interval_ms = max(self.max_interval_ms, interval)
            self.last_at = now
            self.last_frames = frames
            self.count += 1
            self.status_flags += bool(status)
            self.durations.append(frames / self.rate * 1000)

    def dispatched(self, received_at: float) -> None:
        with self._lock:
            age = max(0.0, (time.monotonic() - received_at) * 1000)
            self.dispatches += 1
            self.last_dispatch_ms = age
            self.max_dispatch_ms = max(self.max_dispatch_ms, age)
            self.dispatch_ms.append(age)

    def snapshot(self) -> dict:
        with self._lock:
            intervals, durations, dispatches = (tuple(self.intervals), tuple(self.durations), tuple(self.dispatch_ms))
            counters = {key: getattr(self, key) for key in (
                'count', 'status_flags', 'last_frames', 'max_interval_ms',
                'last_dispatch_ms', 'max_dispatch_ms', 'idle_packets', 'idle_clock_skipped_seconds')}
        def distribution(values):
            items = sorted(tuple(values))
            if not items:
                return {'p50': None, 'p95': None, 'max': None}
            return {'p50': round(items[(len(items)-1)//2], 3),
                    'p95': round(items[min(len(items)-1, math.ceil(.95*len(items))-1)], 3),
                    'max': round(items[-1], 3)}
        return {'writer': 'receive_driven', 'callback_count': counters['count'], 'callback_status_flags': counters['status_flags'],
                'last_callback_frames': counters['last_frames'],
                'callback_packet_ms': distribution(durations),
                'callback_interval_ms': distribution(intervals),
                'max_callback_interval_ms': round(counters['max_interval_ms'], 3),
                'callback_to_ring_ms': distribution(dispatches),
                'last_callback_to_ring_ms': round(counters['last_dispatch_ms'], 3),
                'max_callback_to_ring_ms': round(counters['max_dispatch_ms'], 3),
                'idle_keepalive_packets': counters['idle_packets'],
                'idle_clock_skipped_seconds': round(counters['idle_clock_skipped_seconds'], 3),
                'note': 'Callback-to-ring is application dispatch only; not driver age, HTTP or acoustic latency.'}


class SilenceClock:
    """Keep silent output at source sample rate despite wake-up jitter.

    Real audio resets the cursor and is never delayed. While callbacks are
    absent, pay the elapsed silence debt (not just 10 ms per wake-up). After a
    long scheduler stall cap catch-up to 40 ms and count the discarded gap.
    This isn't a startup/jitter buffer for real sound.
    """
    def __init__(self, step: float, now: float, guard: float = .020):
        if step <= 0 or guard < 0:
            raise ValueError('Invalid silence clock')
        self.step, self.guard, self.cursor = step, guard, now
        self.max_units = max(1, int(.040 / step))
        self.skipped_seconds = 0.0

    def reset(self, now: float) -> None:
        self.cursor = now

    def due(self, now: float) -> int:
        units = max(0, int((now-self.cursor-self.guard+1e-9) / self.step))
        count = min(units, self.max_units)
        if units:
            self.cursor += units*self.step
            self.skipped_seconds += (units-count)*self.step
        return count


def run_receive_driven(capture) -> None:
    """Called by the dedicated writer thread, never by PortAudio or asyncio."""
    unit = capture.frames * capture.channels * 2
    step = capture.frames / capture.rate
    remainder = bytearray()
    remainder_at = None
    silence_clock = SilenceClock(step, time.monotonic(), guard=getattr(capture, 'idle_silence_guard', .050 if getattr(capture, 'continuous_pcm', False) else .020))
    phase = 0
    next_synthetic = time.monotonic()

    def publish(raw: bytes) -> None:
        # Preserve every aligned sample of a normal packet, even when the
        # actual callback is 10 ms despite the requested 1 ms unit size.
        capture.pcm_ring.append_packet(raw)
        capture.written_frames += len(raw) // (capture.channels * 2)

    try:
        if not capture.synthetic and not capture.experimental and capture.start_buffer_seconds:
            capture.stop_event.wait(capture.start_buffer_seconds)
        while not capture.stop_event.is_set():
            if capture.synthetic:
                delay = next_synthetic - time.monotonic()
                if delay > 0 and capture.stop_event.wait(delay):
                    break
                now = time.monotonic()
                if now - next_synthetic > .015:
                    capture.writer_late_ticks += 1
                    next_synthetic = now
                next_synthetic += step
                pcm = array.array('h')
                active = capture.synthetic_duration is None or phase/capture.rate < capture.synthetic_duration
                for i in range(capture.frames):
                    value = int(3200*math.sin((phase+i)*2*math.pi*440/capture.rate)) if active else 0
                    pcm.extend([value]*capture.channels)
                phase += capture.frames
                if os.sys.byteorder != 'little':
                    pcm.byteswap()
                raw = (capture.signal_provider(capture.written_frames, capture.frames, capture.rate, capture.channels)
                       if getattr(capture,'signal_provider',None) else pcm.tobytes())
                publish(raw)
                capture.output_meter.feed(raw)
                continue

            try:
                received_at, raw = capture.queue.get(timeout=.010)
            except queue.Empty:
                now = time.monotonic()
                missing = silence_clock.due(now)
                capture.experimental_timing.idle_clock_skipped_seconds = silence_clock.skipped_seconds
                if missing:
                    raw = bytes(unit*missing)
                    publish(raw)
                    capture.silence_frames += missing*capture.frames
                    capture.experimental_timing.idle_packets += 1
                    capture.output_meter.feed(raw)
                continue
            if capture.stop_event.is_set():
                break
            now = time.monotonic()
            if now - received_at > capture.max_pcm_age:
                capture.stale_frames += len(raw)//(capture.channels*2)
                capture.dropped += 1
                continue
            if remainder and remainder_at is not None and now-remainder_at > capture.max_pcm_age:
                capture.stale_frames += len(remainder)//(capture.channels*2)
                capture.dropped += 1
                remainder.clear()
            capture.last_pcm_age = max(0., now-received_at)
            silence_clock.reset(now)
            if not remainder:
                remainder_at = received_at
            remainder.extend(raw)
            size = len(remainder)//unit*unit
            if size:
                data = bytes(remainder[:size])
                del remainder[:size]
                publish(data)
                capture.experimental_timing.dispatched(received_at)
                capture.output_meter.feed(data)
                if not remainder:
                    remainder_at = None
            # These computations are deliberately outside the audio callback
            # and AFTER publishing sound to the HTTP ring.
            capture.input_meter.feed(raw)
    except (OSError, ValueError, RuntimeError):
        if not capture.stop_event.is_set():
            capture.error = ('PCM остановлен. Нажмите «Основной WAV/PCM · 10 мс» '
                             'и запустите трансляцию заново.')
    finally:
        capture.level = 0
        capture.pcm_ring.wake()
