"""Local control plane; network/audio work stays out of the browser."""
from __future__ import annotations
import asyncio
import collections
import contextlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import time
import webbrowser
import aiohttp
from aiohttp import web
from . import __version__
from .auth import YandexAuth
from .audio import audio_devices
from .loopback import probe_outputs
from .discovery import discover, merge_devices
from .media import MediaServer
from .protocol import BridgeError, GlagolClient, audio_play, say_text
from .security import Settings, get_fingerprint, validate_lan_ip
from .network import interfaces, select_network
from .access import record_matches

MAX_UPLOAD = 256 * 1024 * 1024
WEB_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent)) / 'web'


class Controller:
    def __init__(self, directory: Path):
        self.settings = Settings(directory)
        self.cache = Path(tempfile.mkdtemp(prefix='station-bridge-'))
        self.logs = collections.deque(maxlen=100)
        self.media = MediaServer(self.cache / 'media', self.log)
        self.session = self.auth = self.glagol = None
        self.devices: list[dict] = []
        self.outputs: list[dict] = []
        self.audio_probe: dict = {}
        self.capture_warning_key = ''
        self.live_options = {}
        self.last_resync_at = 0.0
        self.selected = self.settings.data.get('device') or {}
        self.pending_trust: dict | None = None
        self.network: dict = {}
        self.adapters: list[dict] = []
        self.last_audio_command: dict = {}
        self.warned_resource = ''
        self.audio_host_override = str(self.settings.data.get('audio_host_overrides', {}).get(self.selected.get('id'), ''))
        self.qr_task = None
        self.qr_status = 'idle'
        self.qr_svg = ''
        self.qr_started = 0.0
        self.remember = bool(self.settings.data.get('remember', False))
        self.action_lock = asyncio.Lock()
        self.watchdog = None
        self.shutdown = asyncio.Event()
        self.last_ui_seen = time.monotonic()
        self.log('Программа готова. Войдите в Яндекс и найдите Станцию в локальной сети.')

    def log(self, text: str):
        self.logs.append({'time': time.strftime('%H:%M:%S'), 'text': str(text)[:500]})
        print(f'[{time.strftime("%H:%M:%S")}] {text}', flush=True)

    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                                            cookie_jar=aiohttp.CookieJar(quote_cookie=False), trust_env=False)
        self.auth = YandexAuth(self.session)
        self.glagol = GlagolClient(self.session, lambda state: None, self.log)
        if self.remember:
            try:
                self.auth.restore(self.settings.load_account())
                if self.auth.logged_in:
                    self.log('Загружен зашифрованный вход этого пользователя Windows.')
            except Exception:
                self.log('Не удалось прочитать сохранённый вход. Войдите по QR заново.')
        with contextlib.suppress(BridgeError):
            self.outputs = await asyncio.to_thread(audio_devices)
        self.adapters = await asyncio.to_thread(interfaces)
        self.watchdog = asyncio.create_task(self._watch(), name='stream-watchdog')

    async def persist_account(self):
        self.settings.data['remember'] = self.remember
        self.settings.save()
        if self.remember:
            try:
                await asyncio.to_thread(self.settings.save_account, self.auth.export())
            except Exception:
                self.remember = False
                self.settings.data['remember'] = False
                self.settings.save()
                self.log('Не удалось сохранить вход через Windows DPAPI. Он останется только в памяти.')
        else:
            self.settings.forget_account()

    async def _watch(self):
        while True:
            await asyncio.sleep(2)
            if not self.media.token:
                continue
            elapsed = time.monotonic() - self.media.started_at
            if elapsed > 15 and not self.media.requests and self.warned_resource != self.media.token:
                self.warned_resource = self.media.token
                if self.media.rejected_requests:
                    if self.media.peer_policy.pending(self.media.token):
                        self.log('Путь до аудиосервера работает, но адрес отправителя отличается от Glagol. '
                                 'Подтвердите посредника кнопкой «Разрешить адрес и повторить» в панели.')
                    else:
                        self.log('Запросы к аудиосерверу приходят, но отклонены. Причина: ' +
                                 self.media.last_rejection + '. Скачайте диагностику; защита доступа не отключалась.')
                elif self.media.head_requests:
                    self.log('Станция проверила заголовки (HEAD), но не загрузила аудио. '
                             'Проверьте MP3-файл; это уже не отсутствие HTTP-доступа.')
                else:
                    self.log(f'Станция не запросила аудио с {self.media.host}:{self.media.port}. '
                             'Проверьте IP компьютера для аудио, VPN и брандмауэр. '
                             'Игнорирование команды прошивкой также пока не исключено.')
            if self.media.kind != 'live':
                continue
            capture_stats = self.media.capture.stats() if self.media.capture else {}
            if capture_stats.get('source') == 'generated_test' and elapsed > 30 and not self.action_lock.locked():
                async with self.action_lock:
                    await self.stop_playback()
                    self.log('Тест потока завершён. Захват Windows в этом тесте не включался.')
                continue
            if (elapsed > 8 and capture_stats.get('warning')
                    and self.capture_warning_key != self.media.token):
                self.capture_warning_key = self.media.token
                self.log(capture_stats['warning'] + ' Остановите трансляцию и нажмите «Найти выход со звуком».')
            elif capture_stats.get('signal_status') == 'signal' and self.capture_warning_key == self.media.token:
                self.capture_warning_key = ''
                self.log('На выбранном выходе появился аудиосигнал; он передаётся в выбранный аудиопоток.')
            stale = self.media.last_request and time.monotonic() - self.media.last_request > 35
            reason = ''
            if not (self.media.segment_requests or self.media.live_bytes_sent) and elapsed > 40:
                reason = ('Колонка не забрала аудиопоток. Для прямого MP3 попробуйте режим HLS; '
                          'для PCM вернитесь к рабочему MP3; для HLS проверьте тестовый сигнал и доступ к аудиосерверу.')
            elif stale:
                reason = 'Колонка перестала запрашивать аудио. Возможно, Алиса переключилась на другой источник.'
            elif self.media.capture and not self.media.capture.stats()['running']:
                reason = self.media.capture.error or capture_stats.get('warning') or 'Кодирование аудио остановилось.'
            elif time.monotonic() - self.last_ui_seen > 120:
                reason = 'Панель управления закрыта или не отвечает. Захват звука остановлен.'
            if reason and not self.action_lock.locked():
                async with self.action_lock:
                    await self.media.stop()
                    self.log(reason + ' Захват выключен; перезапуск — только вручную.')

    def snapshot(self) -> dict:
        return {'version': __version__, 'windows': os.name == 'nt',
                'auth': {'logged_in': bool(self.auth and self.auth.logged_in),
                         'name': self.auth.display_login if self.auth else '', 'remember': self.remember,
                         'qr_status': self.qr_status},
                'devices': self.devices, 'outputs': self.outputs, 'selected': self.selected,
                'audio_probe': self.audio_probe, 'live_options': self.live_options,
                'trust': self.pending_trust,
                'peer_approval': self.media.peer_policy.pending(self.media.token, challenge=True),
                'station': self.glagol.safe_state() if self.glagol else {},
                'media': self.media.stats(), 'network': {**self.network, 'adapters': self.adapters,
                    'override': self.audio_host_override}, 'last_audio_command': self.last_audio_command,
                'logs': list(self.logs), 'busy': self.action_lock.locked()}

    async def scan(self):
        self.log('Ищем объявления _yandexio._tcp.local. и устройства аккаунта…')
        local_task = asyncio.create_task(discover())
        cloud = []
        if self.auth.logged_in:
            try:
                cloud = await self.auth.devices()
            except BridgeError as exc:
                self.log(str(exc) + ' Продолжаем локальное обнаружение.')
        local = await local_task
        self.devices = merge_devices(cloud, local)
        self.log(f'Найдено в локальной сети: {len(local)}; в аккаунте: {len(cloud)}.')
        return {'devices': self.devices}

    async def begin_qr(self, remember: bool):
        if self.glagol.connected:
            raise BridgeError('Перед сменой аккаунта отключитесь от колонки.')
        if self.qr_task:
            self.qr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.qr_task
        self.remember = remember
        self.qr_status = 'waiting'
        self.qr_svg = ''
        try:
            link = await self.auth.begin_qr()
            import qrcode
            from qrcode.image.svg import SvgPathImage
            qr = qrcode.make(link, image_factory=SvgPathImage, box_size=6, border=4)
            buff = io.BytesIO()
            qr.save(buff)
            self.qr_svg = buff.getvalue().decode('utf-8')
            self.qr_started = time.monotonic()
            self.qr_task = asyncio.create_task(self._poll_qr(), name='qr-login')
            return {'svg': self.qr_svg}
        except Exception:
            self.qr_status = 'error'
            raise

    async def _poll_qr(self):
        try:
            while time.monotonic() - self.qr_started < 180:
                await asyncio.sleep(2.5)
                if await self.auth.poll_qr():
                    self.qr_status = 'done'
                    self.qr_svg = ''
                    await self.persist_account()
                    self.log('Вход в Яндекс выполнен. Теперь нажмите «Найти колонки».')
                    return
            self.qr_status = 'expired'
            self.log('Время QR-входа истекло. Создайте новый QR-код.')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.qr_status = 'error'
            self.log(str(exc) if isinstance(exc, BridgeError) else 'Не удалось завершить QR-вход. Повторите или используйте токен.')

    def validate_device(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            raise BridgeError('Не выбрана колонка.')
        host = validate_lan_ip(str(raw.get('host', '')))
        try:
            port = int(raw.get('port', 1961))
        except (TypeError, ValueError):
            raise BridgeError('Некорректный порт Glagol.')
        if not 1 <= port <= 65535:
            raise BridgeError('Порт Glagol должен быть от 1 до 65535.')
        ident, platform = str(raw.get('id', '')).strip(), str(raw.get('platform', '')).strip()
        if not ident or not platform or max(len(ident), len(platform)) > 256:
            raise BridgeError('Нужны deviceId и platform. Их возвращает поиск mDNS; IP одного недостаточно.')
        return {'id': ident, 'platform': platform, 'host': host, 'port': port,
                'name': str(raw.get('name') or 'Яндекс Станция')[:120]}

    async def connect(self, raw: dict, accepted: str = '', audio_host: str | None = None):
        if not self.auth.logged_in:
            raise BridgeError('Сначала войдите в аккаунт владельца Станции.')
        device = self.validate_device(raw)
        host, port = device['host'], device['port']
        override = (str(self.settings.data.get('audio_host_overrides', {}).get(device['id'], ''))
                    if audio_host is None else str(audio_host).strip())
        fingerprint = await get_fingerprint(host, port)
        pin_key = device['id']
        pins = self.settings.data.setdefault('pins', {})
        known = pins.get(pin_key)
        if known != fingerprint and accepted != fingerprint:
            self.pending_trust = {'device': device, 'fingerprint': fingerprint, 'changed': bool(known), 'audio_host': override}
            return {'trust_required': True, 'trust': self.pending_trust}
        if accepted and (not self.pending_trust or self.pending_trust['device'] != device or
                         self.pending_trust['fingerprint'] != accepted):
            raise BridgeError('Подтверждение сертификата устарело. Повторите подключение.')
        self.pending_trust = None
        await self.disconnect()
        token = await self.auth.device_token(device['id'], device['platform'])
        # An outbound VPN route is not necessarily a reachable HTTP callback address.
        network = await asyncio.to_thread(select_network, host, override)
        host_ip = network['audio_host']
        try:
            await self.media.bind(host_ip, host)
            await self.glagol.connect(host, port, token, fingerprint)
            network['local_http_check'] = await self.media.self_check()
        except BaseException:
            await self.media.close()
            await self.glagol.close()
            raise
        self.selected = device
        self.network = network
        self.adapters = await asyncio.to_thread(interfaces)
        self.audio_host_override = override
        self.settings.data.setdefault('audio_host_overrides', {})[device['id']] = override
        self.settings.data['audio_host'] = host_ip
        self.settings.data['audio_port'] = self.media.port
        self.restore_audio_peer(device, host_ip, fingerprint)
        pins[pin_key] = fingerprint
        self.settings.data['device'] = device
        self.settings.save()
        await self.persist_account()
        self.log(f'Glagol подключён: {device["name"]} · {host}:{port}. Аудиосервер: {host_ip}:8808.')
        for warning in network['warnings']:
            self.log(warning)
        if network['local_http_check']:
            self.log('Аудиосервер отвечает на самом ПК. Доступ со Станции подтвердится только её HTTP-запросом.')
        else:
            self.log('Самопроверка HTTP на ПК не прошла. Проверьте сетевой интерфейс и защитное ПО.')
        return {'connected': True}

    def restore_audio_peer(self, device: dict, host: str, fingerprint: str):
        self.settings.data.pop('active_audio_peer', None)
        records = self.settings.data.get('audio_peer_approvals', {})
        record = records.get(device.get('id')) if isinstance(records, dict) else None
        if not record_matches(record, device, host, fingerprint):
            return
        try:
            self.media.peer_policy.restore(str(record.get('peer', '')))
        except BridgeError:
            self.log('Сохранённый адрес-посредник недействителен; потребуется новое подтверждение.')
            return
        self.settings.data['active_audio_peer'] = dict(record)
        self.log(f'Для этой Станции восстановлено ваше разрешение HTTP-посредника {self.media.peer_policy.approved}.')

    async def approve_audio_peer(self, data: dict):
        # The UI is localhost-only and requires its own per-process secret.
        # Approval challenges expire and are scoped to the current resource.
        url = self.media.current_url()
        peer = self.media.peer_policy.approve(str(data.get('ip', '')),
                                              str(data.get('challenge', '')), self.media.token)
        fingerprint = self.settings.data.get('pins', {}).get(self.selected.get('id'), '')
        record = {'peer': peer, 'station_ip': self.selected.get('host', ''),
                  'audio_host': self.media.host, 'fingerprint': fingerprint}
        if fingerprint and self.selected.get('id'):
            records = self.settings.data.setdefault('audio_peer_approvals', {})
            if not isinstance(records, dict):
                records = self.settings.data['audio_peer_approvals'] = {}
            records[self.selected['id']] = record
            self.settings.data['active_audio_peer'] = dict(record)
            self.settings.save()
        self.log(f'Разрешён HTTP-посредник {peer} только для выбранной Станции и этого IP ПК. '
                 'Случайная ссылка на аудио остаётся обязательной.')
        # Keep exactly the same URL, including for an uploaded file or live HLS.
        # Do not recreate the resource, rotate its token or start another capture.
        self.warned_resource = ''
        result = await self.send_audio(url, self.media.title, hls=self.media.kind == 'live' and self.media.transport == 'hls')
        self.log('Команда воспроизведения повторена с прежней ссылкой. Проверьте запросы аудио и звук.')
        return {'approved': peer, **result}

    async def revoke_audio_peer(self):
        await self.stop_playback()
        self.media.peer_policy.revoke()
        records = self.settings.data.get('audio_peer_approvals', {})
        if isinstance(records, dict):
            records.pop(self.selected.get('id'), None)
        self.settings.data.pop('active_audio_peer', None)
        self.settings.save()
        self.log('Разрешение посредника удалено. Воспроизведение и захват остановлены.')
        return {}

    async def stop_playback(self):
        self.live_options = {}
        if self.glagol.connected and self.media.token:
            with contextlib.suppress(BridgeError):
                await self.glagol.send({'command': 'stop'}, wait=False)
        await self.media.stop()

    async def disconnect(self):
        await self.stop_playback()
        await self.glagol.close()
        await self.media.close()

    def require_connection(self):
        if not self.glagol.connected:
            raise BridgeError('Сначала подключитесь к Станции.')

    async def send_audio(self, url: str, title: str, hls: bool = False):
        self.last_audio_command = {'directive': 'audio_play', 'kind': self.media.kind,
                                   'acknowledged': None, 'time': time.strftime('%H:%M:%S')}
        try:
            result = await self.glagol.send(audio_play(url, title, hls=hls))
        except BridgeError:
            self.last_audio_command['result'] = 'error'
            raise
        self.last_audio_command.update({'acknowledged': bool(result.get('acknowledged')),
                                        'result': 'ack' if result.get('acknowledged') else 'no_ack'})
        return result

    async def play_uploaded(self, source: Path, title: str):
        self.require_connection()
        await self.stop_playback()
        try:
            url = await self.media.play_file_resource(source, title)
            ack = await self.send_audio(url, title)
        except BaseException:
            await self.media.stop()
            raise
        self.log('Файл отправлен на Станцию. Смотрите счётчик HTTP-запросов и проверьте звук.')
        return ack

    async def start_live_playback(self, data: dict, *, synthetic: bool = False):
        try:
            idx = data.get('device_index')
            idx = int(idx) if idx is not None and idx != '' else None
            duration = float(data.get('segment_time', 1))
        except (TypeError, ValueError):
            raise BridgeError('Некорректные параметры звукового выхода.')
        if duration not in (1., 2.):
            raise BridgeError('Допустимы HLS-сегменты длительностью 1 или 2 секунды.')
        transport = str(data.get('transport', 'hls'))
        if transport not in ('mp3', 'hls', 'pcm'):
            raise BridgeError('Выберите PCM, MP3 или HLS.')
        profile = str(data.get('latency_profile', 'balanced'))
        if profile not in ('balanced', 'fast'):
            raise BridgeError('Выберите профиль «Минимум очередей» или «Устойчивый».')
        selection = None
        if not synthetic and idx is not None:
            selection = next((d for d in self.outputs if d['index'] == idx), None)
            if selection is None:
                raise BridgeError('Список звуковых выходов изменился. Обновите список и повторите выбор.')
            if data.get('device_key') and selection.get('key') != data['device_key']:
                raise BridgeError('Аудиовыход изменился после выбора. Обновите список и выберите устройство заново.')
        await self.stop_playback()
        self.capture_warning_key = ''
        name = {'mp3': 'прямой MP3', 'pcm': 'WAV/PCM без кодировщика', 'hls': 'HLS'}[transport]
        self.log(f'Готовим {name}: ' + ('тест без захвата Windows…' if synthetic else 'звук выбранного выхода…'))
        try:
            url = await self.media.start_live(idx, duration, selection=selection, synthetic=synthetic, transport=transport, latency_profile=profile)
            if not synthetic:
                device = self.media.capture.device
                self.log(f'Источник PCM: {device["name"]} · {device["rate"]} Гц · {device["channels"]} канал(а/ов).')
            result = await self.send_audio(url, 'Тест потока' if synthetic else 'Звук компьютера', hls=transport == 'hls')
        except BaseException:
            await self.media.stop()
            raise
        self.live_options = {'device_index': idx, 'device_key': selection.get('key', '') if selection else '',
                             'segment_time': duration, 'transport': transport, 'synthetic': synthetic,
                             'latency_profile': self.media.capture.latency_profile}
        self.last_ui_seen = time.monotonic()
        self.log('Тест потока запущен, остановится автоматически. Захват Windows не включался.' if synthetic else
                 f'Команда {name} отправлена. Буфер самой Станции зависит от прошивки; цифры очередей на ПК — не задержка на слух.')
        return result

    async def action(self, command: str, data: dict) -> dict:
        async with self.action_lock:
            if command == 'qr':
                return await self.begin_qr(bool(data.get('remember')))
            if command == 'token':
                if self.glagol.connected:
                    raise BridgeError('Перед сменой аккаунта отключитесь от Станции.')
                if self.qr_task:
                    self.qr_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self.qr_task
                await self.auth.import_token(str(data.get('token', '')), str(data.get('kind', 'x')))
                self.remember = bool(data.get('remember'))
                self.qr_status = 'done'
                await self.persist_account()
                self.log('Вход по токену выполнен.')
                return {}
            if command == 'logout':
                await self.disconnect()
                if self.qr_task:
                    self.qr_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self.qr_task
                self.qr_status = 'idle'
                self.auth.restore({})
                self.auth.auth_json = {}
                self.auth.auth_headers = {}
                self.session.cookie_jar.clear()
                self.remember = False
                await self.persist_account()
                self.devices = []
                self.pending_trust = None
                self.log('Локальный вход удалён. Для отзыва разрешения используйте настройки аккаунта Яндекса.')
                return {}
            if command == 'scan':
                return await self.scan()
            if command == 'connect':
                return await self.connect(data.get('device', {}), str(data.get('fingerprint', '')),
                                          data.get('audio_host'))
            if command == 'network':
                self.adapters = await asyncio.to_thread(interfaces)
                return {'adapters': self.adapters}
            if command == 'cancel_trust':
                self.pending_trust = None
                return {}
            if command == 'disconnect':
                await self.disconnect()
                self.log('Соединение закрыто, захват выключен.')
                return {}
            if command == 'probe_audio':
                if self.media.kind == 'live':
                    raise BridgeError('Сначала остановите трансляцию, затем включите музыку на ПК и повторите поиск выхода.')
                self.log('Измеряем сигнал на выходах Windows. Микрофоны не открываются; аудиозапись не сохраняется и не передаётся.')
                self.audio_probe = await asyncio.to_thread(probe_outputs)
                self.outputs = self.audio_probe['outputs']
                selected = next((x for x in self.outputs if x['index'] == self.audio_probe['recommended']), None)
                if selected:
                    self.log('Сигнал найден на выходе: ' + selected['name'] + '. Выберите его для трансляции.')
                else:
                    self.log('Сигнал не обнаружен. Запустите музыку, проверьте выход и Mute браузера/плеера в микшере Windows.')
                return self.audio_probe
            if command == 'outputs':
                if self.media.kind == 'live':
                    raise BridgeError('Для обновления списка звуковых выходов сначала остановите трансляцию.')
                self.outputs = await asyncio.to_thread(audio_devices)
                self.audio_probe = {}
                return {'outputs': self.outputs}
            if command == 'stop':
                await self.stop_playback()
                self.log('Воспроизведение и захват остановлены.')
                return {}
            if command == 'shutdown':
                await self.disconnect()
                asyncio.get_running_loop().call_later(.4, self.shutdown.set)
                return {}
            self.require_connection()
            if command == 'approve_audio_peer':
                return await self.approve_audio_peer(data)
            if command == 'revoke_audio_peer':
                return await self.revoke_audio_peer()
            if command == 'test':
                if (self.media.kind == 'test' and self.media.token and not self.media.requests
                        and time.monotonic() - self.media.started_at < 15):
                    self.log('Тестовый сигнал уже отправлен: сохраняем текущую ссылку и ожидаем запрос Станции.')
                    return {'pending': True, 'acknowledged': self.last_audio_command.get('acknowledged')}
                await self.stop_playback()
                try:
                    url = await self.media.test_resource()
                    result = await self.send_audio(url, 'Проверка звука')
                except BaseException:
                    await self.media.stop()
                    raise
                self.log('Отправлен негромкий тестовый сигнал длительностью 3 секунды.')
                return result
            if command == 'resync':
                if self.media.kind != 'live' or not self.live_options:
                    raise BridgeError('Сначала запустите трансляцию.')
                if time.monotonic() - self.last_resync_at < 5:
                    raise BridgeError('Возврат к живому звуку уже выполнен. Не повторяйте его подряд.')
                options = dict(self.live_options)
                self.last_resync_at = time.monotonic()
                self.log('Возвращаемся к живому звуку: старый поток отменяется. Возможна короткая пауза.')
                return await self.start_live_playback(options, synthetic=options.pop('synthetic', False))
            if command in ('live', 'hls_test', 'stream_test'):
                params = dict(data)
                if command == 'hls_test':
                    params['transport'] = 'hls'
                return await self.start_live_playback(params, synthetic=command != 'live')
            if command == 'volume':
                try:
                    volume = float(data.get('value'))
                except (TypeError, ValueError):
                    raise BridgeError('Неверное значение громкости.')
                if not 0 <= volume <= 1:
                    raise BridgeError('Громкость должна быть от 0 до 100%.')
                return await self.glagol.send({'command': 'setVolume', 'volume': volume})
            if command == 'say':
                return await self.glagol.send(say_text(str(data.get('text', ''))))
            if command in ('pause', 'resume'):
                if self.media.kind == 'live':
                    raise BridgeError('Для прямой трансляции используйте «Остановить», затем новый запуск.')
                return await self.glagol.send({'command': 'stop' if command == 'pause' else 'play'})
            raise BridgeError('Неизвестная команда.')

    async def close(self):
        for task in (self.qr_task, self.watchdog):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self.glagol:
            await self.disconnect()
        if self.session:
            await self.session.close()
        await asyncio.to_thread(shutil.rmtree, self.cache, True)


def make_ui_app(controller: Controller, secret: str, port: int) -> web.Application:
    allowed_host = f'127.0.0.1:{port}'
    origin = 'http://' + allowed_host

    @web.middleware
    async def boundary(request, handler):
        if request.remote not in {'127.0.0.1', '::1'} or request.headers.get('Host') != allowed_host:
            raise web.HTTPForbidden()
        if request.headers.get('Origin') not in (None, origin):
            raise web.HTTPForbidden()
        if request.path.startswith('/api/'):
            key = request.headers.get('X-Bridge-Key', '')
            if not secrets.compare_digest(key, secret):
                return web.json_response({'error': 'Ключ панели отсутствует. Откройте панель через Start.cmd.'}, status=401)
        try:
            response = await handler(request)
        except BridgeError as exc:
            controller.log(str(exc))
            response = web.json_response({'error': str(exc)}, status=400)
        except web.HTTPException:
            raise
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            response = web.json_response({'error': 'Некорректные параметры запроса.'}, status=400)
        except Exception as exc:
            # Exception type only: raw messages can contain token-bearing URLs.
            controller.log(f'Внутренняя ошибка {type(exc).__name__}. Перезапустите приложение.')
            response = web.json_response({'error': f'Внутренняя ошибка: {type(exc).__name__}.'}, status=500)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    app = web.Application(middlewares=[boundary], client_max_size=MAX_UPLOAD + 1048576)

    async def state(request):
        controller.last_ui_seen = time.monotonic()
        return web.json_response(controller.snapshot())

    async def action(request):
        if request.content_length and request.content_length > 32768:
            raise web.HTTPRequestEntityTooLarge(max_size=32768, actual_size=request.content_length)
        data = await request.json()
        if not isinstance(data, dict):
            raise BridgeError('Нужен объект JSON.')
        return web.json_response(await controller.action(str(data.get('command', '')), data))

    async def upload(request):
        controller.require_connection()
        async with controller.action_lock:
            reader = await request.multipart()
            field = await reader.next()
            if field is None or field.name != 'file' or not field.filename:
                raise BridgeError('Не выбран аудиофайл.')
            # Browser-supplied filename is used ONLY as a title and suffix, never as a path.
            title = field.filename.replace('\\', '/').split('/')[-1][:180]
            ext = Path(title).suffix.lower()
            if ext not in {'.mp3', '.wav', '.aac', '.flac', '.m4a', '.mp4'}:
                raise BridgeError('Выберите MP3, WAV, AAC, FLAC, M4A или MP4 с аудио.')
            path = controller.cache / ('upload-' + secrets.token_hex(8) + ext)
            count = 0
            try:
                with path.open('wb') as out:
                    while chunk := await field.read_chunk(128 * 1024):
                        count += len(chunk)
                        if count > MAX_UPLOAD:
                            raise BridgeError('Максимальный размер файла — 256 МБ.')
                        await asyncio.to_thread(out.write, chunk)
                if count == 0:
                    raise BridgeError('Файл пуст.')
                return web.json_response(await controller.play_uploaded(path, title))
            finally:
                path.unlink(missing_ok=True)

    async def diagnostic(request):
        state = controller.snapshot()
        # Local IPs are deliberately included for routing diagnosis. No IDs,
        # account data, audio URLs/tokens, QR, certificates or raw station JSON.
        report = {'application': 'Station Bridge', 'version': __version__, 'platform': sys.platform,
                  'connected': state['station'].get('connected'),
                  'playing': state['station'].get('playing'),
                  'media': {k: v for k, v in state['media'].items() if k != 'title'},
                  'network': state['network'], 'last_audio_command': controller.last_audio_command,
                  'audio_outputs': state['outputs'], 'audio_probe': state['audio_probe'],
                  'live_options': controller.live_options,
                  'note': 'Отчёт включает локальные IP, имена сетевых адаптеров и звуковых выходов, уровни сигнала; без токенов, данных аккаунта и аудиозаписей. '
                          'Локальная HTTP-самопроверка не подтверждает доступ со Станции. '
                          'Счётчики — это запросы, а не подтверждение слышимого звука. '
                          'Glagol и HTTP проверяются отдельно. Аппаратная совместимость требует испытания.'}
        return web.json_response(report, headers={'Content-Disposition': 'attachment; filename="StationBridge-diagnostic.json"'})

    async def static(request):
        filename = request.match_info.get('filename') or 'index.html'
        if filename not in {'index.html', 'app.js', 'style.css', 'latency.html', 'latency.js', 'latency.css'}:
            raise web.HTTPNotFound()
        return web.FileResponse(WEB_ROOT / filename)

    app.router.add_get('/api/state', state)
    app.router.add_get('/api/diagnostic', diagnostic)
    app.router.add_post('/api/action', action)
    app.router.add_post('/api/upload', upload)
    app.router.add_get('/', static)
    app.router.add_get('/{filename}', static)
    return app


async def run(directory: Path, open_browser: bool = True, port: int = 8769):
    controller = Controller(directory)
    secret = secrets.token_urlsafe(32)
    runner = None
    try:
        await controller.start()
        app = make_ui_app(controller, secret, port)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=3)
        await runner.setup()
        try:
            await web.TCPSite(runner, '127.0.0.1', port).start()
        except OSError as exc:
            raise BridgeError(f'Порт панели {port} занят. Закройте предыдущий экземпляр Station Bridge.') from exc
        url = f'http://127.0.0.1:{port}/#key={secret}'
        print('\nStation Bridge ' + __version__ + '\nНе закрывайте это окно во время трансляции.\n'
              'Остановка: кнопка «Завершить» в панели или Ctrl+C.\n\nОткрыть панель (ссылка только для вас):\n' + url + '\n', flush=True)
        if open_browser:
            await asyncio.to_thread(webbrowser.open, url)
        await controller.shutdown.wait()
    finally:
        if runner:
            await runner.cleanup()
        await controller.close()
