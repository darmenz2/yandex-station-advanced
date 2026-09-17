"""Discover only advertised Yandex devices; never scan arbitrary hosts."""
from __future__ import annotations
import asyncio
import socket
import threading
from .security import validate_lan_ip
from .protocol import BridgeError


def _discover(seconds: float) -> list[dict]:
    try:
        from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange, IPVersion
    except ImportError as exc:
        raise BridgeError('Не установлен zeroconf. Повторите установку через Start.cmd.') from exc
    result: dict[str, dict] = {}
    lock = threading.Lock()
    zc = Zeroconf(ip_version=IPVersion.V4Only)

    def changed(zeroconf, service_type, name, state_change):
        if state_change == ServiceStateChange.Removed:
            return
        info = zeroconf.get_service_info(service_type, name, timeout=1600)
        if not info:
            return
        props = {k.decode('utf-8', 'replace'): (v.decode('utf-8', 'replace') if isinstance(v, bytes) else v)
                 for k, v in info.properties.items()}
        ident, platform = props.get('deviceId'), props.get('platform')
        if not ident or not platform:
            return
        for addr in info.addresses:
            if len(addr) != 4:
                continue
            try:
                ip = validate_lan_ip(socket.inet_ntoa(addr))
            except BridgeError:
                continue
            with lock:
                result[f'{ident}@{ip}'] = {'id': str(ident), 'platform': str(platform),
                    'host': ip, 'port': info.port, 'name': str(props.get('name') or f'Станция · {ip}')}

    browser = ServiceBrowser(zc, '_yandexio._tcp.local.', handlers=[changed])
    try:
        threading.Event().wait(seconds)
    finally:
        browser.cancel()
        zc.close()
    return sorted(result.values(), key=lambda x: (x['name'], x['host']))


async def discover(seconds: float = 5.0) -> list[dict]:
    return await asyncio.to_thread(_discover, seconds)


def merge_devices(cloud: list[dict], local: list[dict]) -> list[dict]:
    names = {d['id']: d['name'] for d in cloud}
    pairs = {d['id']: d['native_stereo'] for d in cloud if d.get('native_stereo')}
    result = []
    discovered = set()
    for device in local:
        item = dict(device)
        item['name'] = names.get(item['id'], item['name'])
        item['in_account'] = item['id'] in names
        if item['id'] in pairs:item['native_stereo']=pairs[item['id']]
        result.append(item)
        discovered.add(item['id'])
    for device in cloud:
        if device['id'] not in discovered:
            result.append({**device, 'in_account': True})
    return result
