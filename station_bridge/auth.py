"""Yandex QR -> x-token -> music token -> device token.

The login flow is adapted from AlexxIT/YandexStation core/yandex_session.py
(MIT); no password is requested, no browser cookies are imported or persisted.
Public application client identifiers below are from that implementation.
"""
from __future__ import annotations
import asyncio
import json
import re
from typing import Any
import aiohttp
from .protocol import BridgeError

PASSPORT = 'https://passport.yandex.ru/pwl-yandex'
GLAGOL = 'https://quasar.yandex.net/glagol/'


class YandexAuth:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.x_token = ''
        self.music_token = ''
        self.display_login = ''
        self.auth_headers: dict[str, str] = {}
        self.auth_json: dict = {}
        self.token_lock = asyncio.Lock()

    @property
    def logged_in(self) -> bool:
        return bool(self.x_token or self.music_token)

    async def request(self, method: str, url: str, **kwargs) -> Any:
        try:
            async with self.session.request(method, url, **kwargs) as response:
                if response.status >= 400:
                    raise BridgeError(f'Яндекс вернул HTTP {response.status}. Проверьте вход, интернет и повторите попытку.')
                text = await response.text()
                try:
                    return json.loads(text)
                except ValueError as exc:
                    raise BridgeError('Яндекс вернул неожиданный ответ. Возможно, изменилась авторизация или требуется проверка входа.') from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise BridgeError('Нет ответа от Яндекса. Проверьте интернет и повторите вход.') from exc

    async def begin_qr(self) -> str:
        try:
            async with self.session.get(PASSPORT) as response:
                if response.status != 200:
                    raise BridgeError(f'Страница входа Яндекса вернула HTTP {response.status}.')
                text = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise BridgeError('Не удалось открыть страницу входа Яндекса.') from exc
        match = re.search(r'__CSRF__\s*=\s*["\']([^"\']+)', text)
        if not match:
            raise BridgeError('Не найден CSRF для QR-входа. Яндекс мог изменить форму. Доступен вход через x-token интеграции YandexStation.')
        self.auth_headers = {'X-CSRF-Token': match[1]}
        self.auth_json = await self.request('POST', PASSPORT + '/api/passport/auth/password/submit',
                                           json={'retpath': 'https://passport.yandex.ru/'}, headers=self.auth_headers)
        track = self.auth_json.get('track_id')
        if not track:
            raise BridgeError('Яндекс не создал сеанс QR-входа. Повторите попытку.')
        result = await self.request('POST', PASSPORT + '/api/passport/auth/magic/code',
                                    data={'location_id': '0', 'magic_track_id': track, 'track_id': ''}, headers=self.auth_headers)
        link = result.get('link')
        if not isinstance(link, str) or len(link) > 4096:
            raise BridgeError('Яндекс не вернул QR-ссылку.')
        return link

    async def poll_qr(self) -> bool:
        response = await self.request('POST', PASSPORT + '/api/passport/auth/magic/code/status',
                                      json=self.auth_json, headers=self.auth_headers)
        if response.get('state') != 'otp_auth_finished':
            return False
        track = response.get('trackId')
        if not track:
            raise BridgeError('QR подтверждён, но Яндекс не вернул идентификатор сессии.')
        # Upstream checks status only: this endpoint is allowed to return a non-JSON body.
        try:
            async with self.session.post(PASSPORT + '/api/passport/sessions/get_session',
                                         data={'track_id': track}, headers=self.auth_headers) as response:
                if response.status >= 400:
                    raise BridgeError(f'Подтверждение сессии Яндекса вернуло HTTP {response.status}.')
                await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise BridgeError('Не удалось завершить сессию входа Яндекса.') from exc
        cookie = '; '.join(f'{c.key}={c.value}' for c in self.session.cookie_jar
                           if c['domain'].lstrip('.') == 'yandex.ru' or c['domain'].endswith('.yandex.ru'))
        response = await self.request('POST', 'https://mobileproxy.passport.yandex.net/1/bundle/oauth/token_by_sessionid',
            data={'client_id': 'c0ebe342af7d48fbbbfcf2d2eedb8f9e', 'client_secret': 'ad0a908f0aa341a182a37ecd75bc319e'},
            headers={'Ya-Client-Host': 'passport.yandex.ru', 'Ya-Client-Cookie': cookie})
        token = response.get('access_token')
        if not token:
            raise BridgeError('Вход подтверждён, но x-token не получен. Повторите вход.')
        await self.import_token(token, 'x')
        return True

    async def import_token(self, token: str, kind: str = 'x') -> None:
        token = token.strip().removeprefix('OAuth ').strip()
        if not token or len(token) > 8192 or any(c.isspace() for c in token):
            raise BridgeError('Некорректный токен.')
        old = self.export()
        try:
            if kind == 'music':
                self.x_token = ''
                self.music_token = token
                self.display_login = 'Вход по токену Яндекс Музыки'
            elif kind == 'x':
                info = await self.request('GET', 'https://mobileproxy.passport.yandex.net/1/bundle/account/short_info/?avatar_size=islands-300',
                                          headers={'Authorization': f'OAuth {token}'})
                if info.get('status') != 'ok':
                    raise BridgeError('x-token не принят. Обычный токен OAuth-приложения не подходит.')
                self.x_token, self.music_token = token, ''
                self.display_login = str(info.get('display_login') or info.get('login') or 'Яндекс')[:100]
                await self.ensure_music_token()
            else:
                raise BridgeError('Неизвестный тип токена.')
            await self.devices()
        except Exception:
            self.restore(old)
            raise

    async def ensure_music_token(self) -> str:
        async with self.token_lock:
            if self.music_token:
                return self.music_token
            if not self.x_token:
                raise BridgeError('Сначала войдите в аккаунт Яндекса.')
            response = await self.request('POST', 'https://oauth.mobile.yandex.net/1/token', data={
                'client_id': '23cabbbdc6cd418abb4b39c32c41195d', 'client_secret': '53bc75238f0c4d08a118e51fe9203300',
                'grant_type': 'x-token', 'access_token': self.x_token})
            token = response.get('access_token')
            if not token:
                raise BridgeError('Не удалось получить токен для Glagol. Повторите вход по QR.')
            self.music_token = token
            return token

    async def glagol_get(self, path: str, **params) -> dict:
        for attempt in range(2):
            token = await self.ensure_music_token()
            try:
                async with self.session.get(GLAGOL + path, params=params, headers={'Authorization': f'OAuth {token}'}) as response:
                    if response.status in (401, 403) and self.x_token and not attempt:
                        self.music_token = ''
                        continue
                    if response.status != 200:
                        raise BridgeError(f'Glagol API вернул HTTP {response.status}. Войдите в аккаунт владельца Станции.')
                    data = json.loads(await response.text())
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                raise BridgeError('Не удалось получить данные Glagol из Яндекса.') from exc
            if data.get('status') not in (None, 'ok'):
                raise BridgeError('Яндекс не разрешил доступ к устройству. Нужен аккаунт владельца колонки.')
            return data
        raise BridgeError('Токен устарел. Повторите вход.')

    async def devices(self) -> list[dict]:
        result = await self.glagol_get('device_list')
        raw = result.get('devices', [])
        if not isinstance(raw, list):
            raise BridgeError('Изменился формат списка устройств Glagol.')
        devices = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            info = item.get('quasar_info') or item
            device_id = info.get('device_id') or info.get('id')
            platform = info.get('platform')
            if device_id and platform:
                from .native_state import stereo_configuration
                d = {'id': str(device_id), 'platform': str(platform),
                     'name': str(item.get('name') or device_id), 'host': '', 'port': 1961}
                pair=stereo_configuration(item)
                if pair:d['native_stereo']=pair
                devices.append(d)
        return devices

    async def device_token(self, device_id: str, platform: str) -> str:
        data = await self.glagol_get('token', device_id=device_id, platform=platform)
        token = data.get('token')
        if not token:
            raise BridgeError('Яндекс не выдал токен Станции. Проверьте устройство и аккаунт владельца.')
        return str(token)

    def export(self) -> dict:
        return {'x_token': self.x_token, 'music_token': self.music_token, 'display_login': self.display_login}

    def restore(self, data: dict) -> None:
        self.x_token = str(data.get('x_token') or '')
        self.music_token = str(data.get('music_token') or '')
        self.display_login = str(data.get('display_login') or '')
