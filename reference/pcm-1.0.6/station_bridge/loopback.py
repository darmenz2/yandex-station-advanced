"""Select and measure WASAPI *render-loopback* endpoints, never microphones.

A probe is explicit, time-bounded and local: only levels/names leave this module,
no PCM is written to disk or sent over the network. PortAudio's numeric indices
can change across enumerations, so UI selections are verified against names.
"""
from __future__ import annotations

import array
import contextlib
import hashlib
import math
import os
import threading
import time
from collections import deque
from .protocol import BridgeError

SIGNAL_PEAK = 4 / 32768   # a detection threshold, NOT a noise gate on the audio
SIGNAL_RMS = 1 / 32768
MAX_PROBE_DEVICES = 16


def backend():
    if os.name != 'nt':
        raise BridgeError('Проверка аудиовыходов доступна только в Windows.')
    try:
        import pyaudiowpatch as pa
        return pa
    except ImportError as exc:
        raise BridgeError('Не установлен PyAudioWPatch. Повторите установку Start.cmd.') from exc


def levels(pcm: bytes) -> tuple[float, float]:
    """Peak and RMS of signed 16-bit little-endian PCM, across all channels."""
    if not pcm:
        return 0., 0.
    if len(pcm) % 2:
        raise ValueError('Unaligned 16-bit PCM')
    values = array.array('h', pcm)
    if os.sys.byteorder != 'little':
        values.byteswap()
    peak = max(map(abs, values), default=0) / 32768
    rms = math.sqrt(sum(v * v for v in values) / len(values)) / 32768
    return peak, rms


class SignalMeter:
    """Small bounded peak-hold window; a 1.5s UI poll must not miss a 20ms frame."""
    def __init__(self, rate: int, channels: int, *, clock=time.monotonic):
        self.rate, self.channels, self.clock = rate, channels, clock
        self.lock = threading.Lock()
        self.history = deque(maxlen=256)
        self.frames = self.callbacks = self.flags = self.non_silent_frames = 0
        self.peak = self.squares = self.sample_count = 0
        self.last_frame = self.last_signal = None

    def feed(self, pcm: bytes, status: int = 0):
        if len(pcm) % (self.channels * 2):
            raise ValueError('Unaligned PCM channels')
        peak, rms = levels(pcm)
        count = len(pcm) // 2
        frames = count // self.channels
        now = self.clock()
        with self.lock:
            self.callbacks += 1
            self.flags += bool(status)
            if frames:
                self.frames += frames
                self.sample_count += count
                self.squares += rms * rms * count
                self.peak = max(self.peak, peak)
                self.last_frame = now
                self.history.append((now, peak, rms))
                if peak >= SIGNAL_PEAK and rms >= SIGNAL_RMS:
                    self.non_silent_frames += frames
                    self.last_signal = now

    def snapshot(self) -> dict:
        now = self.clock()
        with self.lock:
            while self.history and now - self.history[0][0] > 2.0:
                self.history.popleft()
            return {
                'input_level': round(max((x[1] for x in self.history), default=0.), 6),
                'input_rms': round(max((x[2] for x in self.history), default=0.), 6),
                'peak_since_start': round(self.peak, 6),
                'rms_since_start': round(math.sqrt(self.squares / self.sample_count), 6) if self.sample_count else 0.,
                'callback_count': self.callbacks, 'status_flags': self.flags,
                'received_frames': self.frames,
                'non_silent_seconds': round(self.non_silent_frames / self.rate, 3),
                'last_callback_ago': round(now - self.last_frame, 2) if self.last_frame is not None else None,
                'last_signal_ago': round(now - self.last_signal, 2) if self.last_signal is not None else None,
            }


def describe(p, device: dict, default_index: int = -1) -> dict:
    host = str(p.get_host_api_info_by_index(int(device['hostApi']))['name'])
    name = str(device['name'])
    return {'index': int(device['index']), 'name': name,
            'rate': int(device['defaultSampleRate']), 'channels': int(device['maxInputChannels']),
            'host_api': host, 'default': int(device['index']) == default_index,
            'key': hashlib.sha256((host + '\0' + name).encode('utf-8')).hexdigest()[:24]}


def enumerate_loopbacks(p) -> list[dict]:
    try:
        default_index = int(p.get_default_wasapi_loopback()['index'])
    except (OSError, ValueError, StopIteration):
        default_index = -1
    return [describe(p, d, default_index) for d in p.get_loopback_device_info_generator()
            if d.get('isLoopbackDevice') and int(d.get('maxInputChannels', 0)) > 0]


def audio_devices() -> list[dict]:
    if os.name != 'nt':
        return []
    pa = backend()
    try:
        with pa.PyAudio() as p:
            return enumerate_loopbacks(p)
    except (OSError, ValueError) as exc:
        raise BridgeError('Windows не вернула WASAPI-выходы. Проверьте звуковое устройство.') from exc


def resolve_loopback(p, device_index: int | None = None, selection: dict | None = None) -> dict:
    """Re-resolve an explicit choice by name+host API, never silently pick another."""
    rows = enumerate_loopbacks(p)
    if selection and selection.get('name'):
        matches = [row for row in rows if row['name'] == selection['name']
                   and (not selection.get('host_api') or row['host_api'] == selection['host_api'])]
        if len(matches) != 1:
            raise BridgeError('Выбранный аудиовыход исчез или его имя неоднозначно. Обновите список и выберите выход заново.')
        chosen = matches[0]
    elif device_index is None:
        chosen = next((row for row in rows if row['default']), None)
        if chosen is None:
            raise BridgeError('Нет основного WASAPI-выхода. Выберите аудиоустройство вручную.')
    else:
        chosen = next((row for row in rows if row['index'] == device_index), None)
        if chosen is None:
            raise BridgeError('Нет выбранного loopback-выхода. Микрофоны не поддерживаются.')
    if not 1 <= chosen['channels'] <= 8 or not 8000 <= chosen['rate'] <= 384000:
        raise BridgeError('Неподдерживаемый формат выхода. Выберите стереовыход Windows.')
    return chosen


def recommend(rows: list[dict]) -> dict | None:
    usable = [row for row in rows if not row.get('error') and row.get('signal_detected')]
    if not usable:
        return None
    return max(usable, key=lambda row: (row['non_silent_seconds'], row['rms_since_start'], row['peak_since_start']))


def probe_outputs(duration: float = 4.0) -> dict:
    """Measure all enumerated loopbacks concurrently after an explicit UI click."""
    if not 1.0 <= duration <= 8.0:
        raise BridgeError('Длительность проверки должна быть от 1 до 8 секунд.')
    pa = backend()
    streams, meters, errors = [], {}, {}
    with pa.PyAudio() as p:
        rows = enumerate_loopbacks(p)
        if not rows:
            raise BridgeError('Windows не нашла ни одного WASAPI loopback-выхода.')
        # Keep default first when a machine has many outputs. Report skipped rows.
        order = sorted(rows, key=lambda row: not row['default'])
        try:
            for row in order[:MAX_PROBE_DEVICES]:
                index = row['index']
                if not 1 <= row['channels'] <= 8 or not 8000 <= row['rate'] <= 384000:
                    errors[index] = 'Неподдерживаемый формат выхода.'
                    continue
                meter = meters[index] = SignalMeter(row['rate'], row['channels'])

                def callback(data, frame_count, time_info, status, meter=meter, index=index):
                    try:
                        meter.feed(data or b'', status)
                    except Exception:
                        errors[index] = 'Ошибка чтения PCM при проверке выхода.'
                        return None, pa.paAbort
                    return None, pa.paContinue

                try:
                    stream = p.open(format=pa.paInt16, channels=row['channels'], rate=row['rate'],
                                    input=True, input_device_index=index, frames_per_buffer=max(128, row['rate'] // 50),
                                    stream_callback=callback, start=False)
                    streams.append(stream)
                    stream.start_stream()
                except (OSError, ValueError) as exc:
                    # Do not leak raw exception payloads or callback data.
                    errors[index] = f'Выход не открылся ({type(exc).__name__}); проверьте его доступность.'
            for row in order[MAX_PROBE_DEVICES:]:
                errors[row['index']] = 'Не проверен: больше 16 выходов. Выберите его вручную.'
            time.sleep(duration)
        finally:
            for stream in streams:
                with contextlib.suppress(Exception):
                    stream.stop_stream()
                with contextlib.suppress(Exception):
                    stream.close()
        results = []
        for row in rows:
            stats = meters[row['index']].snapshot() if row['index'] in meters else SignalMeter(max(1, row['rate']), max(1, row['channels'])).snapshot()
            stats['signal_detected'] = stats['non_silent_seconds'] >= .1 and stats['peak_since_start'] >= SIGNAL_PEAK
            results.append({**row, **stats, 'error': errors.get(row['index'], '')})
    selected = recommend(results)
    return {'outputs': rows, 'results': results, 'duration': duration,
            'recommended': selected['index'] if selected else None,
            'recommended_key': selected['key'] if selected else None,
            'note': 'Проверены только выходы loopback. Сохранены уровни, не аудиозапись. Микрофоны не открывались.'}
