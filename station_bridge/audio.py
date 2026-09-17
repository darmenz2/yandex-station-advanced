"""WASAPI loopback -> AAC/HLS or bounded progressive MP3.

Not an audio driver. No microphone input and no silent automatic recording.
The writer is paced and inserts silence when Windows emits no loopback frames.
"""
from __future__ import annotations
import array
import collections
import contextlib
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import wave
from .protocol import BridgeError
from .live_stream import FrameRing, MP3FrameParser
from .realtime import audio_thread_scope
from .loopback import audio_devices, backend, resolve_loopback, SignalMeter
from .experimental_pcm import ReceiveTiming, run_receive_driven
from .stream_notify import NotifiedFrameRing


def ffmpeg_executable() -> str:
    override = os.environ.get('STATION_BRIDGE_FFMPEG')
    if override and Path(override).is_file():
        return str(Path(override).resolve())
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        path = shutil.which('ffmpeg')
        if path:
            return path
        raise BridgeError('Не найден FFmpeg. Повторите установку Start.cmd или задайте STATION_BRIDGE_FFMPEG.')


def make_test_tone(path: Path, duration: float = 3.0) -> None:
    rate = 48000
    samples = array.array('h')
    for i in range(int(rate * duration)):
        t = i / rate
        # Two gentle notes with a short gap; amplitude < -18 dBFS.
        freq = 523.25 if t < 1.35 else 659.25
        gain = min(1., t / .04, max(0., (duration - t) / .12))
        if 1.25 < t < 1.5:
            gain = 0
        s = int(3600 * gain * math.sin(2 * math.pi * freq * t))
        samples.extend((s, s))
    if os.sys.byteorder != 'little':
        samples.byteswap()
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


class HLSCapture:
    def __init__(self, directory: Path, device_index: int | None = None, segment_time: float = 1.0,
                 *, synthetic: bool = False, selection: dict | None = None,
                 synthetic_duration: float | None = None, transport: str = "hls",
                 latency_profile: str = "balanced"):
        if transport not in ("hls", "mp3", "pcm"):
            raise BridgeError("Неизвестный способ передачи звука.")
        if latency_profile not in ('balanced', 'fast', 'experimental'):
            raise BridgeError('Неизвестный профиль задержки.')
        if latency_profile == 'experimental' and transport != 'pcm':
            raise BridgeError('Экспериментальный профиль доступен только для WAV/PCM.')
        self.continuous_pcm = False
        self.experimental = latency_profile == 'experimental'
        self.latency_profile = 'balanced' if transport == 'hls' else latency_profile
        self.block_ms = 1 if self.experimental else (10 if self.latency_profile == 'fast' else 20)
        self.start_buffer_seconds = 0.0 if self.experimental else (.02 if self.latency_profile == 'fast' else .06)
        self.scheduling = {'timer_1ms': False, 'mmcss_audio': False}
        self.input_latency_seconds = None
        self.pcm_ring = None
        self._buffer_received_at = None
        self.transport = transport
        self.mp3 = FrameRing() if transport == "mp3" else None
        self.mp3_thread = None
        self.stale_frames = 0
        self.writer_late_ticks = 0
        self.max_pcm_age = .050 if self.experimental else (0.06 if self.latency_profile == 'fast' else 0.20)
        self.last_pcm_age = 0.0
        self.directory = directory
        self.device_index = device_index
        self.segment_time = segment_time
        self.synthetic = synthetic
        self.selection = selection
        self.synthetic_duration = synthetic_duration
        self.device = {}
        self.input_meter = SignalMeter(48000, 2)
        self.output_meter = SignalMeter(48000, 2)
        self.rate, self.channels = 48000, 2
        self.frames = max(1, round(self.rate * self.block_ms / 1000))
        self.experimental_timing = ReceiveTiming(self.rate) if self.experimental else None
        self.p = self.stream = self.process = None
        self.stop_event = threading.Event()
        self.queue: queue.Queue[tuple[float, bytes]] = queue.Queue(maxsize=16 if self.experimental else 8)
        self.writer = self.stderr_thread = None
        self.level = 0.0
        self.dropped = 0
        self.captured_frames = 0
        self.written_frames = 0
        self.silence_frames = 0
        self.stderr = collections.deque(maxlen=12)
        self.started_at = 0.0
        self.error = ''
        self.lifecycle_lock = threading.RLock()

    def start(self) -> None:
        with self.lifecycle_lock:
            self._start()

    def _start(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        receive_driven = self.experimental or (self.transport == 'pcm' and self.continuous_pcm)
        if self.transport == 'pcm' and self.continuous_pcm:
            # Keep source data through ordinary Windows/UI scheduling stalls.
            # Queue capacity is retention, not intentional playback latency.
            self.max_pcm_age = 2.0
            self.queue = queue.Queue(maxsize=max(32, math.ceil(2000/self.block_ms)))
            self.start_buffer_seconds = 0.0
            self.idle_silence_guard = .250
        if receive_driven and not self.experimental_timing:
            self.experimental_timing = ReceiveTiming(self.rate)
        try:
            if not self.synthetic:
                pa = backend()
                self.p = pa.PyAudio()
                self.device = resolve_loopback(self.p, self.device_index, self.selection)
                self.rate = self.device['rate']
                self.channels = self.device['channels']
                self.input_meter = SignalMeter(self.rate, self.channels)
                self.output_meter = SignalMeter(self.rate, self.channels)
                self.frames = (max(1, round(self.rate / 1000)) if self.experimental
                               else max(128, self.rate * self.block_ms // 1000))
                if receive_driven:
                    self.experimental_timing = ReceiveTiming(self.rate)
                if self.transport == 'pcm' and self.channels not in (1, 2) and not getattr(self, 'allow_multichannel', False):
                    raise BridgeError('Для многоканального PCM задайте вход 5.1/7.1 и роли в Advanced menu.')
                if self.transport == 'pcm' and not 8000 <= self.rate <= 192000:
                    raise BridgeError('PCM поддерживает частоту 8–192 кГц. Для этого выхода используйте MP3.')

                def callback(in_data, frame_count, time_info, status):
                    if self.stop_event.is_set():
                        return None, pa.paComplete
                    try:
                        received_at = time.monotonic()
                        if receive_driven:
                            if in_data and len(in_data) % (self.channels*2):
                                raise ValueError('Unaligned PCM callback')
                            self.experimental_timing.receive(received_at, len(in_data or b'')//(self.channels*2), status)
                        else:
                            self.input_meter.feed(in_data or b'', status)
                        if in_data:
                            self.captured_frames += len(in_data) // (self.channels * 2)
                            if self.queue.full():
                                with contextlib.suppress(queue.Empty):
                                    self.queue.get_nowait()
                                    self.dropped += 1
                            with contextlib.suppress(queue.Full):
                                self.queue.put_nowait((received_at, in_data))
                        if status:
                            self.dropped += 1
                    except Exception:
                        self.error = 'Ошибка чтения PCM выбранного выхода Windows. Обновите список выходов и запустите трансляцию заново.'
                        return None, pa.paAbort
                    return None, pa.paContinue

                self.stream = self.p.open(format=pa.paInt16, channels=self.channels,
                    rate=self.rate, input=True, input_device_index=self.device['index'],
                    frames_per_buffer=self.frames, stream_callback=callback, start=False)
            if self.transport == 'pcm':
                ring_type = NotifiedFrameRing
                self.pcm_ring = ring_type(2048 if self.experimental and self.continuous_pcm else 512 if self.continuous_pcm else 64, frame_bytes=self.frames * self.channels * 2,
                                          frame_seconds=self.frames / self.rate)
                self.started_at = time.monotonic()
                self._start_capture_writer()
                return
            # Raw PCM is fully described. Large generic probing buffers only
            # postpone the first encoded packet; analyzeduration=0 means default.
            command = [ffmpeg_executable(), '-hide_banner', '-loglevel', 'warning', '-nostats',
                '-probesize', '32', '-analyzeduration', '1',
                '-f', 's16le', '-ar', str(self.rate), '-ac', str(self.channels), '-i', 'pipe:0',
                '-vn', '-ac', '2', '-ar', '48000']
            if self.transport == 'mp3':
                command += ['-c:a', 'libmp3lame', '-b:a', '320k', '-reservoir', '0',
                    '-f', 'mp3', '-write_xing', '0', '-id3v2_version', '0',
                    '-flush_packets', '1', 'pipe:1']
            else:
                command += ['-c:a', 'aac', '-b:a', '192k', '-flush_packets', '1',
                    '-f', 'hls', '-hls_time', str(self.segment_time), '-hls_list_size', '6',
                    '-hls_delete_threshold', '8',
                    '-hls_flags', 'delete_segments+omit_endlist+temp_file+independent_segments',
                    '-hls_segment_filename', str(self.directory / 'seg%06d.ts'),
                    str(self.directory / 'live.m3u8')]
            kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE if self.mp3 else subprocess.DEVNULL,
                                            stderr=subprocess.PIPE, bufsize=0, **kwargs)
            self.stderr_thread = threading.Thread(target=self._read_stderr, name='ffmpeg-stderr', daemon=True)
            self.stderr_thread.start()
            self.started_at = time.monotonic()
            if self.mp3:
                self.mp3_thread = threading.Thread(target=self._read_mp3, name='mp3-frames', daemon=True)
                self.mp3_thread.start()
            self._start_capture_writer()
        except Exception as exc:
            self.stop()
            if isinstance(exc, BridgeError):
                raise
            if self.experimental:
                raise BridgeError('Windows не запустила экспериментальный захват с блоком 1 мс. Вернитесь к основному WAV/PCM · 10 мс; автоматического переключения нет.') from exc
            raise BridgeError('Не удалось запустить захват/FFmpeg. Проверьте устройство вывода и установку зависимостей.') from exc

    @property
    def live_ring(self):
        return self.pcm_ring if self.transport == 'pcm' else self.mp3

    @property
    def preroll_frames(self):
        if self.latency_profile in ('fast', 'experimental'):
            return 1 if self.transport == 'pcm' else 2
        return 3 if self.transport == 'pcm' else 6

    def _start_capture_writer(self):
        if self.stream:
            self.stream.start_stream()
            with contextlib.suppress(Exception):
                self.input_latency_seconds = float(self.stream.get_input_latency())
        self.writer = threading.Thread(target=self._write_pcm, name='station-audio-writer', daemon=True)
        self.writer.start()

    def _read_stderr(self):
        for raw in self.process.stderr:
            self.stderr.append(raw.decode('utf-8', 'replace').strip()[:300])

    def _read_mp3(self):
        parser = MP3FrameParser()
        try:
            while not self.stop_event.is_set():
                data = self.process.stdout.read(960)
                if not data:
                    break
                for frame in parser.feed(data):
                    self.mp3.append(frame)
        except (OSError, ValueError):
            if not self.stop_event.is_set():
                self.error = 'Ошибка кодирования прямого MP3. Остановите трансляцию и выберите HLS.'
        finally:
            if not self.stop_event.is_set() and not self.error:
                self.error = 'Прямой MP3-поток завершился. Выберите HLS или запустите заново.'

    def _take_pcm(self, buffer: bytearray, size: int, now: float) -> bytes:
        # Residual bytes from a callback have an age too. A stalled writer
        # must not replay that tail after discarding the timestamped queue.
        if buffer and self._buffer_received_at is not None and now - self._buffer_received_at > self.max_pcm_age:
            self.stale_frames += len(buffer) // (self.channels * 2)
            self.dropped += 1
            buffer.clear()
        if not buffer:
            self._buffer_received_at = None
        while len(buffer) < size:
            try:
                received, data = self.queue.get_nowait()
            except queue.Empty:
                break
            age = now - received
            if age > self.max_pcm_age:
                self.stale_frames += len(data) // (self.channels * 2)
                self.dropped += 1
                continue
            self.last_pcm_age = max(0, age)
            if not buffer:
                self._buffer_received_at = received
            buffer.extend(data)
        if len(buffer) >= size:
            chunk = bytes(buffer[:size])
            del buffer[:size]
            if not buffer:
                self._buffer_received_at = None
            return chunk
        missing = size - len(buffer)
        chunk = bytes(buffer) + bytes(missing)
        buffer.clear()
        self.silence_frames += missing // (self.channels * 2)
        return chunk

    def _write_pcm(self):
        with audio_thread_scope(self.latency_profile in ('fast', 'experimental')) as scheduling:
            self.scheduling = scheduling
            if self.experimental or (self.transport == 'pcm' and self.continuous_pcm):
                run_receive_driven(self)
            else:
                self._write_pcm_loop()

    def _write_pcm_loop(self):
        size = self.frames * self.channels * 2
        silence = bytes(size)
        buffer = bytearray()
        step = self.frames / self.rate
        next_tick = time.monotonic() + self.start_buffer_seconds
        phase = 0
        try:
            while not self.stop_event.is_set():
                delay = next_tick - time.monotonic()
                if delay > 0 and self.stop_event.wait(delay):
                    break
                next_tick += step
                if time.monotonic() - next_tick > (.06 if self.latency_profile == 'fast' else .3):
                    next_tick = time.monotonic() + step
                    self.dropped += 1
                    self.writer_late_ticks += 1
                    self.stale_frames += len(buffer) // (self.channels * 2)
                    buffer.clear()
                if self.synthetic and getattr(self, 'signal_provider', None):
                    chunk = self.signal_provider(self.written_frames, self.frames, self.rate, self.channels)
                elif self.synthetic and (self.synthetic_duration is None or phase / self.rate < self.synthetic_duration):
                    pcm = array.array('h')
                    for i in range(self.frames):
                        sample = int(3200 * math.sin((phase + i) * 2 * math.pi * 440 / self.rate))
                        pcm.extend([sample] * self.channels)
                    phase += self.frames
                    if os.sys.byteorder != 'little':
                        pcm.byteswap()
                    chunk = pcm.tobytes()
                elif self.synthetic:
                    chunk = silence
                else:
                    chunk = self._take_pcm(buffer, size, time.monotonic())
                values = array.array('h', chunk)
                if os.sys.byteorder != 'little':
                    values.byteswap()
                self.level = max((abs(s) for s in values), default=0) / 32768
                self.output_meter.feed(chunk)
                if self.pcm_ring:
                    # WAV is PCM with a header supplied by the HTTP handler.
                    # No encoder, resampler, pipe or disk is used on this path.
                    self.pcm_ring.append(chunk)
                else:
                    # A raw pipe write is allowed to be partial on Windows.
                    view = memoryview(chunk)
                    while view and not self.stop_event.is_set():
                        count = self.process.stdin.write(view)
                        if not count:
                            raise BrokenPipeError()
                        view = view[count:]
                self.written_frames += self.frames
        except (BrokenPipeError, OSError, ValueError):
            if not self.stop_event.is_set():
                self.error = 'FFmpeg остановился или звуковое устройство отключено. Остановите и запустите трансляцию заново.'
        finally:
            self.level = 0

    def ready(self, segments: int = 3) -> bool:
        if self.live_ring:
            return self.live_ring.snapshot()['encoded_frames'] >= self.preroll_frames
        try:
            text = (self.directory / 'live.m3u8').read_text('utf-8')
            return text.count('#EXTINF:') >= segments
        except (OSError, UnicodeError):
            return False

    def stats(self) -> dict:
        process_active = (bool(self.pcm_ring) if self.transport == 'pcm'
                          else bool(self.process and self.process.poll() is None))
        writer_active = bool(self.writer and self.writer.is_alive())
        capture_active = bool(self.synthetic)
        if self.stream:
            try:
                capture_active = bool(self.stream.is_active())
            except (OSError, ValueError):
                capture_active = False
        alive = process_active and writer_active and capture_active and not self.error and not self.stop_event.is_set()
        signal = self.input_meter.snapshot()
        encoded = self.output_meter.snapshot()
        elapsed = time.monotonic() - self.started_at if self.started_at else 0.
        status = 'generated_test' if self.synthetic else 'starting'
        warning = ''
        if not self.synthetic and elapsed >= 3:
            if not capture_active:
                status = 'inactive'
                warning = 'Windows остановила захват этого аудиовыхода. Выберите доступный выход и перезапустите трансляцию.'
            elif signal['last_callback_ago'] is None or signal['last_callback_ago'] > 2:
                status = 'no_frames'
                warning = 'От выбранного выхода не поступают аудиокадры. Включите музыку и проверьте, куда выводит звук браузер/плеер.'
            elif signal['last_signal_ago'] is None or signal['last_signal_ago'] > 2:
                status = 'silent'
                warning = 'Выбранный выход отдаёт тишину. Проверьте его громкость, Mute и выход браузера/плеера в микшере Windows.'
            else:
                status = 'signal'
        ring = self.mp3.snapshot() if self.mp3 else {}
        return {'transport': self.transport,
                'latency_profile': self.latency_profile,
                'experimental': self.experimental,
                'experimental_timing': self.experimental_timing.snapshot() if self.experimental_timing else None,
                'requested_block_ms': self.block_ms,
                'actual_block_ms': round(self.frames / self.rate * 1000, 2),
                'block_duration_note': 'Размер блока программы; фактические интервалы callback смотрите отдельно.',
                'startup_jitter_ms': round(self.start_buffer_seconds * 1000),
                'preroll_ms': round(self.preroll_frames * self.live_ring.frame_seconds * 1000, 1) if self.live_ring else None,
                'input_latency_seconds': self.input_latency_seconds,
                'scheduling': dict(self.scheduling),
                'encoder_bypassed': self.transport == 'pcm',
                'pcm': self.pcm_ring.snapshot() if self.pcm_ring else None,
                'pcm_queue_blocks': self.queue.qsize(),
                'pcm_queue_limit_ms': round(self.max_pcm_age * 1000),
                'last_pcm_age_ms': round(self.last_pcm_age * 1000, 1),
                'stale_pcm_seconds': round(self.stale_frames / self.rate, 3),
                'writer_late_ticks': self.writer_late_ticks,
                'mp3': ring or None,
                'encoder_backlog_seconds': (round(max(0, self.written_frames / self.rate -
                                                       ring['encoded_seconds']), 3) if ring else None),
                'first_encoded_frame_seconds': (round(self.mp3.first_frame_at - self.started_at, 3)
                    if self.mp3 and self.mp3.first_frame_at is not None else None),
                'running': bool(alive), 'level': encoded['input_level'], 'dropped_blocks': self.dropped,
                'captured_seconds': round(self.captured_frames / self.rate, 1),
                'stream_seconds': round(self.written_frames / self.rate, 1),
                'inserted_silence_seconds': round(self.silence_frames / self.rate, 1), 'error': self.error,
                'source': 'generated_test' if self.synthetic else 'wasapi_loopback',
                'device': self.device, 'capture_active': capture_active,
                'encoder_active': process_active if self.transport != 'pcm' else False, 'writer_active': writer_active,
                'signal_status': status, 'warning': warning, **signal,
                'output_peak_since_start': encoded['peak_since_start'],
                'output_non_silent_seconds': encoded['non_silent_seconds']}

    def stop(self) -> None:
        with self.lifecycle_lock:
            self._stop()

    def _stop(self) -> None:
        self.stop_event.set()
        if self.stream:
            with contextlib.suppress(Exception):
                self.stream.stop_stream()
            with contextlib.suppress(Exception):
                self.stream.close()
            self.stream = None
        if self.p:
            with contextlib.suppress(Exception):
                self.p.terminate()
            self.p = None
        if self.writer and self.transport == "pcm":
            self.writer.join(timeout=2)
        if self.process:
            if self.process.poll() is None:
                # Killing unblocks a writer stuck in a full pipe; do not close its fd concurrently.
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            if self.writer:
                self.writer.join(timeout=2)
            if self.stderr_thread:
                self.stderr_thread.join(timeout=2)
            if self.mp3_thread:
                self.mp3_thread.join(timeout=2)
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                if pipe:
                    with contextlib.suppress(OSError):
                        pipe.close()
        self.level = 0
