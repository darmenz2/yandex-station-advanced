"""One producer, independent HTTP readers and Glagol sessions per speaker.

This is concurrent fan-out, NOT sample-synchronised Yandex multiroom.
Every speaker has a unique URL, certificate pin, ACL and TCP port. A slow
or disconnected reader cannot stop another reader or own the capture source.
"""
from __future__ import annotations
import asyncio
import contextlib
from dataclasses import dataclass
import secrets
import time
from station_bridge.app import Controller
from station_bridge.media import MediaServer
from station_bridge.audio import audio_devices
from station_bridge.protocol import BridgeError, GlagolClient, audio_play
from station_bridge.network import select_network
from station_bridge.security import get_fingerprint
from station_bridge.access import record_matches
from station_bridge.continuity import ResumeGate, MaintenancePolicy
from . import __version__

MAX_SPEAKERS = 16


class BorrowedMedia(MediaServer):
    """Per-speaker presentation. Never closes the shared source/directory."""
    async def stop(self):
        self.token = ''
        self.peer_policy.clear_pending()
        tasks = list(self._live_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.capture = self.directory = None
        self.resource_name = self.kind = ''

    async def attach(self, owner: MediaServer):
        # First stop this reader only. Owner's audio can be shared by >1 consumer.
        await self.stop()
        if not owner.token or owner.directory is None:
            raise BridgeError('Общий аудиопоток уже остановлен.')
        self.token = secrets.token_urlsafe(18)
        self.capture, self.directory = owner.capture, owner.directory
        self.capture_id = owner.capture_id
        self.kind, self.title = owner.kind, owner.title
        self.transport, self.resource_name = owner.transport, owner.resource_name
        self.latency_profile = owner.latency_profile
        self.playback_guard_ms = owner.playback_guard_ms
        self.started_at = time.monotonic()
        self.last_request = self.requests = self.incoming_requests = 0
        self.head_requests = self.rejected_requests = self.segment_requests = 0
        self.bytes_requested = self.live_bytes_sent = self.live_connections = 0
        self.live_skipped_frames = self.live_max_write_ms = self.live_write_queue_bytes = 0
        self.last_rejection = self.last_close_reason = ''
        self.last_close_at = 0
        self.encoder_ready_seconds = owner.encoder_ready_seconds
        self.first_audio_request_seconds = None
        self.stream_events.clear()
        self.last_steady_stats=None;self.pacing_stalls=0;self.live_write_stalls=0
        return self.current_url()


@dataclass
class Peer:
    device: dict
    glagol: GlagolClient
    media: MediaServer
    network: dict
    gate: ResumeGate
    policy: MaintenancePolicy
    error: str = ''
    blocked: bool = False


class GroupController(Controller):
    desktop_mode = True

    def __init__(self, directory):
        super().__init__(directory)
        self.peers: dict[str, Peer] = {}
        self.group_trust: dict[str, dict] = {}
        self.endpoint = None
        self.endpoint_state = {'available': False, 'endpoints': [], 'error': ''}
        self.endpoint_task = None
        self.last_launch_options = self.settings.data.get('desktop_launch_options') or {'playback_mode': 'main'}
        self.driver_busy = False

    def primary(self):
        return Peer(self.selected, self.glagol, self.media, self.network,
                    self.resume_gate, self.maintenance, blocked=self.recovery_blocked)

    def all_peers(self):
        return ([self.primary()] if self.glagol and self.selected and self.media.runner else []) + list(self.peers.values())

    async def start(self):
        await super().start()
        from .windows_audio import AudioWorker
        self.endpoint = AudioWorker()
        self.endpoint_task = asyncio.create_task(self._endpoint_monitor())

    async def _endpoint_monitor(self):
        while True:
            try:
                self.endpoint_state = await self.endpoint.status(self.settings.data.get('desktop_endpoint_id', ''))
            except Exception as exc:
                self.endpoint_state = {'available': False, 'endpoints': [], 'error': type(exc).__name__}
            await asyncio.sleep(1)

    async def connect_group(self, raw_devices, accepted=None, overrides=None):
        if self.media.kind == 'live' and not getattr(self, '_replace_group', False):
            raise BridgeError('Остановите трансляцию перед изменением состава группы.')
        if not isinstance(raw_devices, list) or not 1 <= len(raw_devices) <= MAX_SPEAKERS:
            raise BridgeError('Выберите от 1 до 16 колонок.')
        devices = [self.validate_device(d) for d in raw_devices]
        if len({d['id'] for d in devices}) != len(devices):
            raise BridgeError('Одна Станция выбрана несколько раз. Оставьте один её адрес.')
        if not self.auth.logged_in:
            raise BridgeError('Войдите по QR в аккаунт владельца Станций.')
        accepted, overrides = accepted or {}, overrides or {}
        if not isinstance(accepted, dict) or not isinstance(overrides, dict):
            raise BridgeError('Некорректное подтверждение группы.')
        pins = self.settings.data.get('pins') or {}
        pending, verified = {}, []
        # Certificates are fetched before credentials and before changing playback.
        for d in devices:
            if hasattr(self,'stage'):self.stage(d['name']+': проверяем сертификат…')
            try:fp = await get_fingerprint(d['host'], d['port'])
            except BridgeError as exc:
                if hasattr(self,'connection_errors'):self.connection_errors[d['id']]=str(exc)
                raise BridgeError(d['name']+': '+str(exc)) from exc
            override = str(overrides.get(d['id'], self.settings.data.get('audio_host_overrides', {}).get(d['id'], ''))).strip()
            prior = self.group_trust.get(d['id'])
            confirmed = (accepted.get(d['id']) == fp and prior and
                         prior['device'] == d and prior['fingerprint'] == fp and prior['audio_host'] == override)
            if pins.get(d['id']) != fp and not confirmed:
                pending[d['id']] = {'device': d, 'fingerprint': fp, 'changed': bool(pins.get(d['id'])), 'audio_host': override}
            verified.append((d, fp, override))
        if pending:
            self.group_trust = pending
            return {'trust_required': True, 'trust': list(pending.values())}
        await self.disconnect()
        d, fp, override = verified[0]
        self.pending_trust = {'device': d, 'fingerprint': fp, 'audio_host': override}
        if hasattr(self,'stage'):self.stage(d['name']+': подключаем управление и аудиосервер…')
        await super().connect(d, fp, override)
        results = [{'id': d['id'], 'connected': True}]
        for slot, (d, fp, override) in enumerate(verified[1:], 1):
            if hasattr(self,'stage'):self.stage(d['name']+': подключаем колонку '+str(slot+1)+' из '+str(len(devices))+'…')
            client = GlagolClient(self.session, lambda state: None,
                                  lambda message, name=d['name']: self.log(name + ': ' + message))
            media = BorrowedMedia(self.cache / ('peer-' + str(slot)), self.log)
            try:
                network = await asyncio.to_thread(select_network, d['host'], override)
                token = await self.auth.device_token(d['id'], d['platform'])
                await media.bind(network['audio_host'], d['host'], 8808+slot)
                await client.connect(d['host'], d['port'], token, fp)
                now = time.monotonic()
                peer = Peer(d, client, media, network, ResumeGate(now), MaintenancePolicy(now, self.maintenance.interval))
                self.peers[d['id']] = peer
                self.settings.data.setdefault('pins', {})[d['id']] = fp
                self.settings.data.setdefault('audio_host_overrides', {})[d['id']] = override
                record = (self.settings.data.get('audio_peer_approvals') or {}).get(d['id'])
                if record_matches(record, d, media.host, fp):
                    media.peer_policy.restore(record['peer'])
                results.append({'id': d['id'], 'connected': True})
                self.log(f'{d["name"]}: подключена; аудиопорт {media.port}.')
            except BaseException as exc:
                await media.close()
                await client.close()
                if isinstance(exc,asyncio.CancelledError):raise
                text = str(exc) if isinstance(exc, BridgeError) else type(exc).__name__
                results.append({'id': d['id'], 'connected': False, 'error': text})
                self.log(d['name'] + ': ' + text)
        self.group_trust = {}
        self.settings.data['desktop_firewall_peers'] = [{'station':p.device['host'], 'host':p.media.host, 'port':p.media.port} for p in self.all_peers()]
        self.settings.data['desktop_group'] = devices
        self.settings.save()
        return {'results': results, 'synchronization': 'independent_players'}

    async def stop_playback(self):
        for peer in list(self.peers.values()):
            if peer.glagol.connected and peer.media.token:
                with contextlib.suppress(BridgeError):
                    await peer.glagol.send({'command': 'stop'}, wait=False)
            await peer.media.stop()
            peer.gate.reset(time.monotonic())
            peer.policy.reset(time.monotonic())
            peer.blocked = False
        await super().stop_playback()

    async def disconnect(self):
        await self.stop_playback()
        peers, self.peers = list(self.peers.values()), {}
        for peer in peers:
            await peer.glagol.close()
            await peer.media.close()
        await super().disconnect()

    async def send_audio(self, url, title, hls=False):
        # Attach before dispatch. One Windows capture/encoder, N independent HTTP readers.
        tasks = []
        names = [self.selected.get('name', 'Станция')]
        peers = list(self.peers.values())
        for peer in peers:
            child_url = await peer.media.attach(self.media)
            peer.blocked = False
            peer.gate.reset(time.monotonic())
            peer.policy.reset(time.monotonic())
            tasks.append((peer.glagol, audio_play(child_url, title, hls)))
            names.append(peer.device['name'])
        tasks = [client.send(payload) for client, payload in tasks]
        tasks.insert(0, Controller.send_audio(self, url, title, hls))
        answers = await asyncio.gather(*tasks, return_exceptions=True)
        report = []
        for index, (name, answer) in enumerate(zip(names, answers)):
            error = isinstance(answer, BaseException)
            text = (str(answer) if isinstance(answer, BridgeError) else type(answer).__name__) if error else ''
            if index:
                peers[index-1].error = text
            if error:
                self.log(name + ': ' + text)
            report.append({'name': name, 'sent': not error, 'acknowledged': bool(not error and answer.get('acknowledged')), 'error': text})
        if not any(x['sent'] for x in report):
            raise BridgeError('Ни одна Станция не приняла отправку команды. Проверьте подключения.')
        return {'acknowledged': any(x['acknowledged'] for x in report), 'group_results': report}

    def may_refresh(self, peer):
        state = peer.glagol.safe_state()
        if state.get('playing') and state.get('title') and state['title'] not in ('Звук компьютера', peer.media.title):
            peer.blocked = True
            if peer.media is self.media:
                self.recovery_blocked = True
        return (not peer.blocked and bool(state.get('connected')) and bool(state.get('playing')) and
                state.get('state_age_seconds', 1e9) < 15 and state.get('alice', '') in ('', 'IDLE'))

    async def refresh_peer(self, peer, reason):
        url = await peer.media.refresh_live_url(reason)
        peer.policy.refreshed(time.monotonic(), reason)
        if reason == 'manual':
            peer.blocked = False
            if peer.media is self.media:
                self.recovery_blocked = False
        self.log(peer.device.get('name', 'Станция') + ': живой звук (' + reason + ').')
        return await peer.glagol.send(audio_play(url, peer.media.title))

    async def soft_resync(self, reason):
        self.last_resync_at = time.monotonic()
        tasks = []
        for p in self.all_peers():
            if p.media.capture and p.media.transport in ('pcm', 'mp3') and p.glagol.connected:
                if reason in ('manual', 'micro_buffer_changed') or self.may_refresh(p):
                    p.media.playback_guard_ms = self.media.playback_guard_ms
                    tasks.append(self.refresh_peer(p, reason))
        answers = await asyncio.gather(*tasks, return_exceptions=True)
        return {'refreshed': sum(not isinstance(a, BaseException) for a in answers)}

    async def continuity_step(self):
        capture = self.media.capture
        if not capture or capture.synthetic or self.media.transport not in ('pcm', 'mp3'):
            return
        now = time.monotonic()
        if self.action_lock.locked() or now < self.calibration_until:
            return
        signal = capture.input_meter.snapshot()
        for p in self.all_peers():
            if not p.media.capture:
                continue
            allowed = self.may_refresh(p)
            p.policy.interval = self.maintenance.interval
            reason = 'sound_after_silence' if p.gate.observe(now, signal, enabled=self.auto_live, suspended=not allowed) else ''
            if not reason:
                age = signal.get('last_signal_ago')
                reason = p.policy.observe(now, suspended=not allowed, playing=allowed,
                    has_signal=age is not None and age < .5, readers=len(p.media._live_tasks),
                    ever_sent=bool(p.media.live_bytes_sent), last_data_age=now-p.media.last_request,
                    closed_age=now-p.media.last_close_at, close_reason=p.media.last_close_reason)
            if reason:
                async with self.action_lock:
                    if self.media.capture is not capture:
                        return
                    with contextlib.suppress(BridgeError):
                        await self.refresh_peer(p, reason)

    async def _watch(self):
        while True:
            await asyncio.sleep(2)
            # A running tray, not an open browser tab, owns the user's capture session.
            self.last_ui_seen = time.monotonic()
            if self.media.kind != 'live' or not self.media.capture:
                continue
            elapsed = time.monotonic()-self.media.started_at
            states = [p.media for p in self.all_peers()]
            active = any(m.last_request and time.monotonic()-m.last_request < 35 for m in states)
            stats = self.media.capture.stats()
            reason = ''
            if stats.get('source') == 'generated_test' and elapsed > 30:
                reason = 'Тест завершён.'
            elif not stats.get('running'):
                reason = stats.get('error') or 'Захват остановился.'
            elif elapsed > 45 and not active:
                reason = 'Все колонки перестали забирать аудио; захват остановлен.'
            if reason and not self.action_lock.locked():
                async with self.action_lock:
                    await self.stop_playback()
                    self.log(reason)

    async def start_live_playback(self, data, *, synthetic=False):
        data = dict(data)
        if not synthetic and self.settings.data.get('desktop_endpoint_id'):
            from .windows_audio import choose_loopback
            chosen = self.endpoint_state.get('selected')
            if not chosen:
                raise BridgeError('Выбранный аудиовыход Windows недоступен. Переподключите его и обновите устройства.')
            output = choose_loopback(chosen['name'], self.outputs)
            data.update(device_index=output['index'], device_key=output['key'])
        result = await super().start_live_playback(data, synthetic=synthetic)
        if not synthetic:
            self.last_launch_options = {k: v for k, v in data.items() if k in ('device_index', 'device_key', 'playback_mode', 'transport', 'latency_profile', 'segment_time')}
            self.settings.data['desktop_launch_options'] = self.last_launch_options
            self.settings.save()
        return result

    async def bind_endpoint(self, ident):
        if self.media.kind == 'live':
            raise BridgeError('Остановите трансляцию перед сменой аудиовыхода.')
        if ident:
            status = await self.endpoint.status(ident)
            if not status.get('selected'):
                raise BridgeError('Выбранный выход больше не доступен.')
            self.outputs = await asyncio.to_thread(audio_devices)
            from .windows_audio import choose_loopback
            choose_loopback(status['selected']['name'], self.outputs)
            self.endpoint_state = status
        self.settings.data['desktop_endpoint_id'] = str(ident)
        self.settings.save()
        return {}

    def advanced_snapshot(self):
        rows = []
        for p in self.all_peers():
            rows.append({'device': p.device, 'state': p.glagol.safe_state(), 'media': p.media.stats(),
                         'peer_approval': p.media.peer_policy.pending(p.media.token, challenge=True),
                         'error': p.error, 'primary': p.media is self.media})
        return {'application': 'Yandex Station Advanced', 'version': __version__, 'peers': rows,
                'native_audio': self.endpoint_state, 'saved_group': self.settings.data.get('desktop_group', []),
                'last_launch_options': self.last_launch_options, 'group_trust': list(self.group_trust.values()),
                'synchronization': 'Concurrent independent players; no sample-accurate sync.',
                'driver_busy': self.driver_busy}

    def snapshot(self):
        result = super().snapshot()
        result['version'] = __version__
        return result

    async def action(self, command, data):
        if command == 'calibration_lease' and not data.get('release') and data.get('purpose') != 'local_video' and len(self.all_peers()) > 1:
            raise BridgeError('Для микрофонного замера оставьте одну Станцию: несколько источников дают неоднозначное эхо.')
        return await super().action(command, data)

    async def advanced_action(self, command, data):
        async with self.action_lock:
            if command == 'firewall':
                if data.get('confirmed') is not True:
                    raise BridgeError('Нужно подтверждение сетевых правил для этой группы.')
                from .native_helper import firewall
                await asyncio.to_thread(firewall)
                return {'launched': True}
            if command == 'sound_settings':
                from .native_helper import sound_settings
                await asyncio.to_thread(sound_settings)
                return {}
            if command == 'install_driver':
                if data.get('confirmed') is not True:
                    raise BridgeError('Подтвердите загрузку и запуск оригинального установщика VB-CABLE.')
                from .native_helper import install_driver
                await asyncio.to_thread(install_driver)
                return {'launched': True}
            if command == 'rename_endpoint':
                from .native_helper import rename_endpoint
                selected = self.endpoint_state.get('selected') or {}
                if self.media.kind == 'live':
                    raise BridgeError('Остановите трансляцию перед переименованием выхода.')
                if data.get('confirmed') is not True or not selected.get('virtual_cable'):
                    raise BridgeError('Нужно подтверждение и выбранный виртуальный выход VB-CABLE.')
                await asyncio.to_thread(rename_endpoint, selected['id'])
                return {'launched': True}

            if command == 'connect_group':
                return await self.connect_group(data.get('devices'), data.get('accepted'), data.get('overrides'))
            if command == 'bind_endpoint':
                return await self.bind_endpoint(data.get('id', ''))
            if command in ('master_volume', 'master_mute'):
                ident = self.settings.data.get('desktop_endpoint_id')
                if not ident:
                    raise BridgeError('Сначала выберите аудиовыход Windows для Станций.')
                await self.endpoint.set(ident, volume=data.get('value') if command == 'master_volume' else None,
                                        mute=bool(data.get('value')) if command == 'master_mute' else None)
                self.endpoint_state = await self.endpoint.status(ident)
                return {}
            if command == 'native_refresh':
                self.endpoint_state = await self.endpoint.status(self.settings.data.get('desktop_endpoint_id', ''))
                self.outputs = await asyncio.to_thread(audio_devices)
                return {}
            if command == 'cancel_group_trust':
                self.group_trust = {}
                return {}
            if command == 'station_volume':
                value = data.get('value')
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                    raise BridgeError('Громкость должна быть 0–100%.')
                p = next((p for p in self.all_peers() if p.device['id'] == data.get('id')), None)
                if p is None:
                    raise BridgeError('Станция не подключена.')
                return await p.glagol.send({'command': 'setVolume', 'volume': round(value, 1)})
            if command == 'peer_approve':
                p = next((p for p in self.all_peers() if p.device['id'] == data.get('id')), None)
                if p is None:
                    raise BridgeError('Станция не подключена.')
                peer_ip = p.media.peer_policy.approve(str(data.get('ip', '')), str(data.get('challenge', '')), p.media.token)
                rec = {'peer': peer_ip, 'station_ip': p.device['host'], 'audio_host': p.media.host,
                       'fingerprint': self.settings.data.get('pins', {}).get(p.device['id'], '')}
                self.settings.data.setdefault('audio_peer_approvals', {})[p.device['id']] = rec
                self.settings.save()
                return await p.glagol.send(audio_play(p.media.current_url(), p.media.title, p.media.transport == 'hls'))
            if command == 'peer_revoke':
                p = next((p for p in self.all_peers() if p.device['id'] == data.get('id')), None)
                if not p:
                    raise BridgeError('Станция не подключена.')
                # Stop entire shared session before revoking any primary/follower grant.
                await self.stop_playback()
                p.media.peer_policy.revoke()
                self.settings.data.setdefault('audio_peer_approvals', {}).pop(p.device['id'], None)
                self.settings.save()
                return {}
        raise BridgeError('Неизвестная команда группы.')

    async def close(self):
        if self.endpoint_task:
            self.endpoint_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.endpoint_task
        if self.endpoint:
            self.endpoint.close()
        await super().close()
