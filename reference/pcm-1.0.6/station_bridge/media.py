"""A small, IP-restricted media server. Never exposes settings or arbitrary files."""
from __future__ import annotations
import asyncio
import contextlib
from pathlib import Path
import re
import secrets
import shutil
import time
import socket
from aiohttp import web, ClientSession, ClientTimeout, ClientError
from .audio import HLSCapture, make_test_tone
from .protocol import BridgeError
from .access import AudioPeerPolicy
from .live_stream import FRAME_BYTES, FRAME_SECONDS, RESPONSE_FRAMES
from .pcm_stream import wav_header, pcm_response_blocks

SAFE_NAME = re.compile(r'^(live\.m3u8|live\.mp3|live\.wav|seg[0-9]{6,12}\.ts|audio\.(mp3|aac|wav|flac|mp4|m4a))$')
TYPES = {'.m3u8': 'application/vnd.apple.mpegurl', '.ts': 'video/mp2t', '.mp3': 'audio/mpeg',
         '.aac': 'audio/aac', '.wav': 'audio/wav', '.flac': 'audio/flac', '.mp4': 'audio/mp4', '.m4a': 'audio/mp4'}


class MediaServer:
    def __init__(self, cache: Path, log):
        self.cache, self.log = cache, log
        self.cache.mkdir(parents=True, exist_ok=True)
        self.runner = None
        self.host = ''
        self.station_ip = ''
        self.port = 8808
        self.token = ''
        self.directory: Path | None = None
        self.capture: HLSCapture | None = None
        self.kind = ''
        self.requests = 0
        self.incoming_requests = 0
        self.head_requests = 0
        self.rejected_requests = 0
        self.last_peer = ''
        self.last_rejection = ''
        self.segment_requests = 0
        self.bytes_requested = 0
        self.last_request = 0.0
        self.started_at = 0.0
        self.title = ''
        self.resource_name = ''
        self.peer_policy = AudioPeerPolicy()
        self.transport = 'hls'
        self.live_bytes_sent = 0
        self.live_connections = 0
        self.live_skipped_frames = 0
        self.live_max_write_ms = 0.0
        self.live_send_lag_ms = None
        self.first_audio_request_seconds = None
        self.encoder_ready_seconds = None
        self.last_segment_number = None
        self.hls_request_lag_seconds = None
        self._live_tasks: set[asyncio.Task] = set()
        self.response_frames = RESPONSE_FRAMES
        self.pcm_response_blocks = None  # optional finite budget for tests
        self.live_write_queue_bytes = 0
        self.live_socket_buffer_bytes = None
        self.latency_profile = 'balanced' 

    async def bind(self, host: str, station_ip: str, port: int = 8808):
        if self.runner:
            await self.close()
        self.host, self.station_ip, self.port = host, station_ip, port
        self.peer_policy.configure(host, station_ip)
        # Count attempts BEFORE token/peer checks. A zero GET count alone did
        # not distinguish a firewall timeout from a request rejected here.
        @web.middleware
        async def audit(request, handler):
            if request.path == '/health':
                return await handler(request)
            self.incoming_requests += 1
            self.last_peer = request.remote or ''
            try:
                return await handler(request)
            except web.HTTPException as exc:
                if exc.status >= 400:
                    self.rejected_requests += 1
                    self.last_rejection = ('peer_not_allowed' if exc.status == 403 else
                                           'missing_or_expired_resource' if exc.status == 404 else
                                           f'http_{exc.status}')
                    if self.rejected_requests == 1:
                        self.log(f'Аудиозапрос отклонён: {self.last_rejection}; источник {self.last_peer}.')
                raise
        app = web.Application(middlewares=[audit])
        app.router.add_get('/health', self.health)
        app.router.add_get('/m/{token}/{name}', self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        try:
            site = web.TCPSite(self.runner, host, port)
            await site.start()
        except OSError as exc:
            await self.runner.cleanup()
            self.runner = None
            raise BridgeError(f'Нельзя открыть аудиосервер {host}:{port}. Порт занят или выбран неверный сетевой интерфейс.') from exc

    async def health(self, request: web.Request):
        # No file listing, tokens, playback controls or device details.
        if request.remote not in {self.station_ip, self.host, '127.0.0.1'}:
            raise web.HTTPForbidden()
        return web.json_response({'service': 'Station Bridge audio', 'ok': True},
                                 headers={'Cache-Control': 'no-store'})

    async def self_check(self) -> bool:
        """Local-only server check. It is NOT a reverse-connectivity test."""
        try:
            async with ClientSession(timeout=ClientTimeout(total=3), trust_env=False) as session:
                async with session.get(f'http://{self.host}:{self.port}/health') as response:
                    body = await response.json()
                    return response.status == 200 and body.get('service') == 'Station Bridge audio'
        except (ClientError, OSError, TimeoutError, ValueError, AttributeError):
            return False

    async def handle(self, request: web.Request):
        # Validate capability URL and file BEFORE proposing an unexpected peer.
        # Proxy headers cannot identify the speaker; only request.remote is used.
        token = request.match_info['token']
        name = request.match_info['name']
        if not self.token or not secrets.compare_digest(token, self.token) or not SAFE_NAME.fullmatch(name):
            raise web.HTTPNotFound()
        directory = self.directory
        if directory is None:
            raise web.HTTPNotFound()
        path = directory / name
        is_live_mp3 = (name == 'live.mp3' and self.transport == 'mp3'
                       and self.capture is not None and self.capture.mp3 is not None)
        is_live_pcm = (name == 'live.wav' and self.transport == 'pcm'
                       and self.capture is not None and self.capture.pcm_ring is not None)
        if not (is_live_mp3 or is_live_pcm) and (not path.is_file() or path.is_symlink() or path.resolve().parent != directory.resolve()):
            raise web.HTTPNotFound()
        peer = request.remote or ''
        if not self.peer_policy.allows(peer):
            if self.peer_policy.observe(peer, self.token):
                self.log(f'Запрос к текущему аудио пришёл с {peer}, а Glagol подключён к {self.station_ip}. '
                         'В панели появилось подтверждение адреса-посредника. До разрешения аудио не выдаётся.')
            raise web.HTTPForbidden(text='Audio peer approval required in the local control panel.')
        if is_live_mp3 or is_live_pcm:
            return await self.handle_mp3(request, token, self.capture)
        if request.method == 'HEAD':
            self.head_requests += 1
        if request.method == 'GET':
            self.requests += 1
            self.last_request = time.monotonic()
            if name.endswith('.ts'):
                self.segment_requests += 1
                if self.first_audio_request_seconds is None:
                    self.first_audio_request_seconds = round(time.monotonic() - self.started_at, 3)
                self.last_segment_number = int(name[3:-3])
                # Publication-to-request estimate only; NOT the speaker's sound delay.
                if self.capture:
                    segment_end = (self.last_segment_number + 1) * self.capture.segment_time
                    self.hls_request_lag_seconds = round(max(0, self.capture.written_frames /
                        self.capture.rate - segment_end), 3)
            with contextlib.suppress(OSError):
                self.bytes_requested += path.stat().st_size
            if self.requests == 1:
                self.log(f'Разрешённый клиент запросил аудио с {peer}: сервер выдаёт файл. Проверьте слышимость.')
            if self.segment_requests == 1:
                self.log('Станция запросила первый HLS-сегмент. Проверьте, слышен ли звук.')
        # FileResponse supplies exact Content-Length, byte ranges and HEAD semantics.
        # Stored files / HLS use true finite file sizes; MP3 has its own handler.
        return web.FileResponse(path, headers={
            'Content-Type': TYPES[path.suffix], 'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff'})

    async def handle_mp3(self, request: web.Request, token: str, capture: HLSCapture):
        """Finite-length progressive stream, restarting at live on each GET.

        This resource is a live presentation, not a seekable stored file. Range
        is deliberately ignored with 200 and Accept-Ranges: none. A cancelled
        stream is aborted, never silently marked as a completed response.
        """
        ring = capture.live_ring
        pcm = capture.transport == 'pcm'
        budget = (self.pcm_response_blocks or pcm_response_blocks(capture.rate, capture.channels, capture.frames)) if pcm else self.response_frames
        header = wav_header(capture.rate, capture.channels, budget * ring.frame_bytes) if pcm else b''
        headers = {'Content-Type': 'audio/wav' if pcm else 'audio/mpeg',
                   'Content-Length': str(len(header) + budget * ring.frame_bytes),
                   'Cache-Control': 'no-store, no-cache, must-revalidate', 'Accept-Ranges': 'none',
                   'X-Content-Type-Options': 'nosniff', 'Connection': 'close'}
        if request.method == 'HEAD':
            self.head_requests += 1
            return web.Response(headers=headers)
        if len(self._live_tasks) >= 2:
            raise web.HTTPServiceUnavailable(text='Too many live audio readers', headers={'Retry-After': '1'})
        response = web.StreamResponse(headers=headers)
        response.force_close()
        task = asyncio.current_task()
        self._live_tasks.add(task)
        self.requests += 1
        self.live_connections += 1
        self.last_request = time.monotonic()
        sent_frames = 0
        complete = False
        try:
            transport = request.transport
            if transport is not None:
                fast = capture.latency_profile == 'fast'
                transport.set_write_buffer_limits(high=4096 if fast else 8192, low=1024 if fast else 2048)
                sock = transport.get_extra_info('socket')
                if sock:
                    with contextlib.suppress(OSError):
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096 if fast else 16384)
                        self.live_socket_buffer_bytes = sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
            await response.prepare(request)
            # Do not replay PCM captured during authorisation or Glagol delays.
            if header:
                await asyncio.wait_for(response.write(header), 2)
            preroll = capture.preroll_frames
            cursor = ring.live_cursor(preroll)
            if self.live_connections == 1:
                self.log('Станция открыла прямой ' + ('WAV/PCM' if pcm else 'MP3') + '-поток. Слышимость и задержку проверьте отдельно.')
            last_data = time.monotonic()
            while (self.token == token and self.capture is capture and not capture.stop_event.is_set()
                   and sent_frames < budget):
                batch = 1 if capture.latency_profile == 'fast' else 3
                read = ring.read(cursor, min(batch, budget - sent_frames))
                lag_limit = max(preroll + 1, int((.096 if capture.latency_profile == 'fast' else .48) / ring.frame_seconds))
                if read.newest - read.cursor > lag_limit or read.skipped:
                    # Drop stale independent frames, never send a multi-second backlog.
                    fresh = ring.live_cursor(preroll)
                    self.live_skipped_frames += max(0, fresh - cursor)
                    cursor = fresh
                    read = ring.read(cursor, min(batch, budget - sent_frames))
                if not read.data:
                    if capture.error or (capture.process is not None and capture.process.poll() is not None) or time.monotonic() - last_data > 3:
                        break
                    await asyncio.sleep(.002 if capture.latency_profile == "fast" else .01)
                    continue
                begin = time.monotonic()
                await asyncio.wait_for(response.write(read.data), 2)
                last_data = time.monotonic()
                if self.token != token:
                    break
                self.live_max_write_ms = max(self.live_max_write_ms, (last_data - begin) * 1000)
                self.live_send_lag_ms = round((read.newest - read.cursor) * ring.frame_seconds * 1000, 1)
                cursor = read.cursor
                sent_frames += len(read.data) // ring.frame_bytes
                if request.transport is not None:
                    self.live_write_queue_bytes = request.transport.get_write_buffer_size()
                self.live_bytes_sent += len(read.data)
                self.bytes_requested += len(read.data)
                self.last_request = last_data
                if self.first_audio_request_seconds is None:
                    self.first_audio_request_seconds = round(last_data - self.started_at, 3)
            complete = sent_frames == budget
            if complete:
                await response.write_eof()
        except (ConnectionError, OSError, asyncio.TimeoutError, RuntimeError):
            # A probe GET may intentionally disconnect; capture remains bounded.
            pass
        finally:
            self._live_tasks.discard(task)
            if not complete and request.transport is not None:
                request.transport.abort()
        return response

    async def new_resource(self, kind: str, title: str):
        await self.stop()
        if self.runner is None:
            raise BridgeError('Сначала подключите Станцию: аудиосервер ещё не запущен.')
        self.token = secrets.token_urlsafe(18)
        self.directory = self.cache / self.token
        self.directory.mkdir(parents=True)
        self.kind, self.title = kind, title
        self.requests = self.segment_requests = self.bytes_requested = 0
        self.incoming_requests = self.head_requests = self.rejected_requests = 0
        self.last_peer = self.last_rejection = '' 
        self.last_request = 0
        self.live_bytes_sent = self.live_connections = self.live_skipped_frames = 0
        self.live_max_write_ms = 0.0
        self.live_send_lag_ms = None
        self.live_write_queue_bytes = 0
        self.live_socket_buffer_bytes = None
        self.first_audio_request_seconds = self.encoder_ready_seconds = None
        self.last_segment_number = self.hls_request_lag_seconds = None
        self.started_at = time.monotonic()

    def url(self, name: str) -> str:
        return f'http://{self.host}:{self.port}/m/{self.token}/{name}'

    async def play_file_resource(self, source: Path, title: str) -> str:
        ext = source.suffix.lower()
        if ext not in {'.mp3', '.aac', '.wav', '.flac', '.mp4', '.m4a'}:
            raise BridgeError('Поддерживаются MP3, AAC, WAV, FLAC, M4A и MP4 с аудио.')
        await self.new_resource('file', title)
        target = self.directory / ('audio' + ext)
        await asyncio.to_thread(shutil.copyfile, source, target)
        self.resource_name = target.name
        return self.url(target.name)

    async def test_resource(self) -> str:
        await self.new_resource('test', 'Проверка звука')
        await asyncio.to_thread(make_test_tone, self.directory / 'audio.wav')
        self.resource_name = 'audio.wav'
        return self.url('audio.wav')

    async def start_live(self, device_index: int | None, segment_time: float = 1, *,
                         selection: dict | None = None, synthetic: bool = False, transport: str = 'hls',
                         latency_profile: str = 'balanced'):
        if transport not in ('mp3', 'hls', 'pcm'):
            raise BridgeError('Выберите PCM, MP3 или HLS.')
        if latency_profile not in ('balanced', 'fast'):
            raise BridgeError('Неизвестный профиль задержки.')
        await self.new_resource('live', 'Тест потока' if synthetic else 'Звук компьютера')
        self.transport = transport
        self.capture = HLSCapture(self.directory, device_index, segment_time, selection=selection,
                                  synthetic=synthetic, synthetic_duration=18 if synthetic else None, transport=transport,
                                  latency_profile=latency_profile)
        try:
            await asyncio.to_thread(self.capture.start)
            deadline = time.monotonic() + 14
            while time.monotonic() < deadline:
                if self.capture.error or not self.capture.stats()['running']:
                    raise BridgeError(self.capture.error or 'Источник звука остановился при подготовке потока.')
                if self.capture.ready():
                    self.encoder_ready_seconds = round(time.monotonic() - self.started_at, 3)
                    self.resource_name = {'mp3': 'live.mp3', 'pcm': 'live.wav', 'hls': 'live.m3u8'}[transport]
                    return self.url(self.resource_name)
                await asyncio.sleep(.01 if latency_profile == "fast" else .1)
            raise BridgeError('Не сформировался аудиопоток. Проверьте FFmpeg и устройство звука.')
        except BaseException:
            await self.stop()
            raise

    def current_url(self) -> str:
        if not self.token or not self.directory or not self.resource_name:
            raise BridgeError('Аудиоресурс уже остановлен. Запустите проверку звука заново.')
        if self.resource_name not in ('live.mp3', 'live.wav') and not (self.directory / self.resource_name).is_file():
            raise BridgeError('Аудиофайл больше не доступен. Запустите проверку звука заново.')
        return self.url(self.resource_name)

    def stats(self) -> dict:
        return {'active': bool(self.token), 'kind': self.kind, 'title': self.title,
                'host': self.host, 'port': self.port, 'requests': self.requests,
                'incoming_requests': self.incoming_requests, 'head_requests': self.head_requests,
                'rejected_requests': self.rejected_requests, 'last_peer': self.last_peer,
                'last_rejection': self.last_rejection,
                'approved_audio_peer': self.peer_policy.approved,
                'pending_audio_peer': self.peer_policy.pending(self.token),
                'segment_requests': self.segment_requests, 'requested_mb': round(self.bytes_requested / 1048576, 2),
                'last_request_ago': round(time.monotonic() - self.last_request, 1) if self.last_request else None,
                'elapsed': round(time.monotonic() - self.started_at, 1) if self.token else 0,
                'capture': self.capture.stats() if self.capture else None,
                'transport': self.transport if self.kind == 'live' else None,
                'live_connections': self.live_connections, 'live_active_readers': len(self._live_tasks),
                'live_bytes_sent': self.live_bytes_sent,
                'live_skipped_frames': self.live_skipped_frames,
                'live_max_write_ms': round(self.live_max_write_ms, 2),
                'live_write_queue_bytes': self.live_write_queue_bytes,
                'live_socket_buffer_bytes': self.live_socket_buffer_bytes,
                'live_send_lag_ms': self.live_send_lag_ms,
                'encoder_ready_seconds': self.encoder_ready_seconds,
                'first_audio_request_seconds': self.first_audio_request_seconds,
                'last_segment_number': self.last_segment_number,
                'hls_request_lag_seconds': self.hls_request_lag_seconds,
                'latency_note': 'Показатели очередей и запросов на ПК; не измерение задержки слышимого звука.'}

    async def stop(self):
        self.token = ''  # revoke URL before stopping producer
        self.resource_name = ''
        self.peer_policy.clear_pending()
        tasks = list(self._live_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        capture, directory = self.capture, self.directory
        self.capture = None
        self.directory = None
        self.kind = ''
        if capture:
            await asyncio.to_thread(capture.stop)
        if directory:
            await asyncio.to_thread(shutil.rmtree, directory, True)

    async def close(self):
        await self.stop()
        self.peer_policy.revoke()
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
