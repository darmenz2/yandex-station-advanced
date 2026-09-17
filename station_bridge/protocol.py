"""Small, independently packaged Glagol client.

Protocol shapes adapted from AlexxIT/YandexStation (MIT, see licenses/).
In particular use audio_play, not the obsolete radio_play directive.
"""
from __future__ import annotations
import asyncio
import base64
import contextlib
import json
import time
import uuid
from collections import deque
from .native_state import observe_app_state
from typing import Any, Callable
import aiohttp


class BridgeError(Exception):
    """An error whose message may be safely displayed to the user."""


def varint(n: int) -> bytes:
    if not isinstance(n, int) or n < 0:
        raise ValueError('varint needs a nonnegative integer')
    out = bytearray()
    while n > 127:
        out.append((n & 127) | 128)
        n >>= 7
    out.append(n)
    return bytes(out)


def proto_string(field: int, value: str) -> bytes:
    raw = value.encode('utf-8')
    return varint(field * 8 + 2) + varint(len(raw)) + raw


def external_command(name: str, payload: dict[str, Any]) -> dict[str, str]:
    data = proto_string(1, name) + proto_string(2, json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
    return {'command': 'externalCommandBypass', 'data': base64.b64encode(data).decode('ascii')}


def audio_play(url: str, title: str = 'Звук компьютера', hls: bool = False) -> dict[str, str]:
    if not url.startswith(('http://', 'https://')) or len(url) > 250:
        raise BridgeError('Ссылка должна начинаться с http(s) и быть не длиннее 250 символов.')
    return external_command('audio_play', {
        'stream': {'url': url, 'format': 'HLS' if hls else 'MP3',
                   'type': 'FmRadio' if hls else 'Track', 'id': url},
        'metadata': {'title': title[:160], 'subtitle': 'Station Bridge'},
    })


def say_text(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text or len(text) > 1000:
        raise BridgeError('Введите текст длиной от 1 до 1000 символов.')
    return {'command': 'serverAction', 'serverActionEventPayload': {
        'type': 'server_action', 'name': 'update_form', 'payload': {
            'form_update': {'name': 'personal_assistant.scenarios.quasar.iot.repeat_phrase',
                            'slots': [{'type': 'string', 'name': 'phrase_to_repeat', 'value': text}]},
            'resubmit': True}}}


def envelope(token: str, payload: dict[str, Any], request_id: str | None = None) -> dict[str, Any]:
    return {'conversationToken': token, 'id': request_id or str(uuid.uuid4()),
            'payload': payload, 'sentTime': int(time.time() * 1000)}


class GlagolClient:
    def __init__(self, session: aiohttp.ClientSession, notify: Callable[[dict], None], log: Callable[[str], None]):
        self.session, self.notify, self.log = session, notify, log
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.reader: asyncio.Task | None = None
        self.waiters: dict[str, asyncio.Future] = {}
        self.token = ''
        self.state: dict[str, Any] = {}
        self.last_message = 0.0
        self.last_state_at = 0.0
        self.state_event = asyncio.Event()
        self.closing = False
        self.send_lock = asyncio.Lock()
        self.native_observation = {}
        self.native_history = deque(maxlen=120)
        self._native_last_at = 0.0

    @property
    def connected(self) -> bool:
        return self.ws is not None and not self.ws.closed

    async def connect(self, host: str, port: int, token: str, fingerprint: str) -> None:
        await self.close()
        self.closing = False
        self.state = {}
        self.native_observation = {}; self.native_history.clear(); self._native_last_at = 0.0
        self.state_event.clear()
        self.token = token
        try:
            self.ws = await asyncio.wait_for(self.session.ws_connect(
                f'wss://{host}:{port}/', ssl=aiohttp.Fingerprint(bytes.fromhex(fingerprint)),
                heartbeat=25, max_msg_size=2 * 1024 * 1024), timeout=12)
        except aiohttp.ServerFingerprintMismatch as exc:
            raise BridgeError('Сертификат колонки изменился. Подключитесь заново и проверьте отпечаток.') from exc
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            raise BridgeError(f'Не удалось открыть Glagol {host}:{port}. Проверьте IP, сеть и изоляцию Wi-Fi.') from exc
        self.reader = asyncio.create_task(self._receive(), name='glagol-reader')
        # Listener MUST run and waiter MUST exist before a request is sent.
        await self.send({'command': 'softwareVersion'}, wait=False)
        try:
            await asyncio.wait_for(self.state_event.wait(), 10)
        except asyncio.TimeoutError as exc:
            await self.close()
            raise BridgeError('WebSocket открыт, но Станция не прислала состояние. Проверьте вход и токен устройства.') from exc

    async def _receive(self) -> None:
        try:
            async for msg in self.ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type == aiohttp.WSMsgType.ERROR:
                        break
                    continue
                try:
                    data = json.loads(msg.data)
                except (ValueError, TypeError):
                    continue
                if not isinstance(data, dict):
                    continue
                self.last_message = time.monotonic()
                native = observe_app_state(data)
                if native:
                    self.native_observation = native
                    if self.last_message - self._native_last_at >= .5:
                        self._native_last_at = self.last_message
                        self.native_history.append({'received_at_monotonic':round(self.last_message,6), **native})
                request_id = data.get('requestId')
                future = self.waiters.get(request_id)
                if future is not None and not future.done():
                    future.set_result(data)
                if isinstance(data.get('state'), dict):
                    self.state = data['state']
                    self.last_state_at = time.monotonic()
                    self.state_event.set()
                    self.notify(self.safe_state())
        except (aiohttp.ClientError, OSError):
            pass
        finally:
            for future in self.waiters.values():
                if not future.done():
                    future.set_exception(BridgeError('Соединение со Станцией прервано.'))
            if not self.closing:
                self.log('Glagol отключён. Для восстановления нажмите «Подключиться».')
                self.notify({'connected': False})

    async def send(self, payload: dict[str, Any], wait: bool = True) -> dict[str, Any]:
        if not self.connected:
            raise BridgeError('Сначала подключитесь к Станции.')
        rid = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future() if wait else None
        if future is not None:
            self.waiters[rid] = future
        try:
            async with self.send_lock:
                await self.ws.send_json(envelope(self.token, payload, rid))
            if future is None:
                return {'acknowledged': False}
            try:
                data = await asyncio.wait_for(future, 7)
            except asyncio.TimeoutError:
                # Some firmwares do not ACK externalCommandBypass. HTTP requests
                # are the independent evidence that audio actually reached a player.
                self.log('Команда отправлена без подтверждения; проверяем запросы аудио от Станции.')
                return {'acknowledged': False}
            status = str(data.get('status', '')).upper()
            if status and status not in ('SUCCESS', 'OK', '0'):
                # Never copy raw device JSON: it can contain authentication material.
                raise BridgeError('Станция отклонила команду (статус не SUCCESS). Проверьте журнал и прошивку.')
            return {'acknowledged': bool(status)}
        except (aiohttp.ClientError, ConnectionError) as exc:
            raise BridgeError('Ошибка отправки команды по Glagol.') from exc
        finally:
            self.waiters.pop(rid, None)

    def safe_state(self) -> dict[str, Any]:
        state = self.state
        player = state.get('playerState') or {}
        return {'connected': self.connected, 'playing': bool(state.get('playing')),
                'state_age_seconds': round(time.monotonic()-self.last_state_at, 2) if self.last_state_at else 1e9,
                'volume': state.get('volume'), 'alice': str(state.get('aliceState', ''))[:50],
                'title': str(player.get('title', ''))[:180], 'subtitle': str(player.get('subtitle', ''))[:180]}

    async def close(self) -> None:
        self.closing = True
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
        if self.reader and self.reader is not asyncio.current_task():
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.reader
        self.reader = None
        self.ws = None
        self.token = ''
