"""Choose a LAN callback address, not just the OS outbound/VPN source address.

ifaddr is already installed by zeroconf. Selection is a heuristic, NOT proof
of a reverse route. No routes, adapters or firewall settings are modified.
"""
from __future__ import annotations

import ipaddress
import socket
from .protocol import BridgeError
from .security import local_ip_for, validate_lan_ip

# Interface names are only hints. Users can override all automatic choices.
VIRTUAL_HINTS = ('wintun', 'wireguard', 'tailscale', 'zerotier', 'vpn', 'clash',
                 'sing-box', 'v2ray', 'nekoray', 'mihomo', 'vmware', 'virtualbox',
                 'vethernet', 'hyper-v', 'loopback', 'docker', 'hamachi',
                 'tun0', 'tun1', 'tun2', 'tap-windows', 'utun')


def interfaces() -> list[dict]:
    """Return actual local IPv4s with their actual prefix, never assume /24."""
    found: dict[str, dict] = {}
    try:
        import ifaddr
        for adapter in ifaddr.get_adapters():
            name = str(adapter.nice_name or adapter.name)
            for item in adapter.ips:
                if not isinstance(item.ip, str):  # IPv6 is a tuple in ifaddr
                    continue
                try:
                    address = validate_lan_ip(item.ip)
                    prefix = int(item.network_prefix)
                    if not 0 <= prefix <= 32:
                        continue
                except (BridgeError, ValueError, TypeError):
                    continue
                found[address] = {'address': address, 'prefix': prefix, 'name': name[:140],
                                  'virtual_hint': any(h in name.lower() for h in VIRTUAL_HINTS)}
    except (ImportError, OSError, RuntimeError):
        pass
    if not found:
        # Fail-soft fallback. Unknown masks stay unknown: no guessed subnet.
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                try:
                    address = validate_lan_ip(info[4][0])
                    found[address] = {'address': address, 'prefix': None,
                                      'name': 'Локальный адрес (маска неизвестна)', 'virtual_hint': False}
                except BridgeError:
                    continue
        except OSError:
            pass
    return sorted(found.values(), key=lambda row: int(ipaddress.IPv4Address(row['address'])))


def on_link(address: str, prefix: int | None, station_ip: str) -> bool:
    if prefix is None:
        return False
    try:
        network = ipaddress.IPv4Network(f'{address}/{prefix}', strict=False)
        return ipaddress.IPv4Address(station_ip) in network
    except ValueError:
        return False


def choose_address(station_ip: str, adapters: list[dict], route_ip: str = '', override: str = '') -> dict:
    """Pure selection logic, also used by regression tests with VPN topologies."""
    station_ip = validate_lan_ip(station_ip)
    candidates = []
    for raw in adapters:
        try:
            address = validate_lan_ip(str(raw.get('address', '')))
            prefix = raw.get('prefix')
            if prefix is not None:
                prefix = int(prefix)
                if not 0 <= prefix <= 32:
                    continue
        except (BridgeError, TypeError, ValueError):
            continue
        if address == station_ip:
            continue  # Never advertise the speaker as the PC.
        candidates.append({'address': address, 'prefix': prefix,
                           'name': str(raw.get('name', ''))[:140],
                           'virtual_hint': bool(raw.get('virtual_hint')),
                           'same_subnet': on_link(address, prefix, station_ip),
                           'route_selected': address == route_ip})
    try:
        route_ip = validate_lan_ip(route_ip) if route_ip else ''
    except BridgeError:
        route_ip = ''
    if route_ip == station_ip:
        route_ip = ''
    override = override.strip()
    if override:
        address = validate_lan_ip(override)
        if address == station_ip:
            raise BridgeError('В поле IP компьютера указан IP Станции. Нужен адрес Wi-Fi/Ethernet самого ПК.')
        selected = next((x for x in candidates if x['address'] == address),
                        {'address': address, 'prefix': None, 'name': 'Указан вручную',
                         'same_subnet': False, 'virtual_hint': False, 'route_selected': address == route_ip})
        mode = 'manual'
    else:
        direct = [x for x in candidates if x['same_subnet'] and not x['virtual_hint']]
        direct.sort(key=lambda x: (not x['route_selected'], -x['prefix'], x['address']))
        if direct:
            selected, mode = direct[0], 'same_subnet'
        elif route_ip:
            selected = next((x for x in candidates if x['address'] == route_ip),
                            {'address': route_ip, 'prefix': None, 'name': 'Исходящий маршрут Windows',
                             'same_subnet': False, 'virtual_hint': False, 'route_selected': True})
            mode = 'route_fallback'
        else:
            raise BridgeError('Не удалось выбрать адрес аудиосервера. Укажите локальный IP компьютера вручную.')
    warnings = []
    if selected['address'] != route_ip and route_ip:
        warnings.append(f'Исходящий маршрут выбирает {route_ip}; для аудио выбран {selected["address"]}. '
                        'При VPN разрешите доступ к локальной сети. Сам VPN программа не меняет.')
    if not selected['same_subnet']:
        warnings.append('Общая подсеть ПК и Станции не подтверждена. Маршрутизация между подсетями возможна, '
                        'но обратный доступ Станции к ПК ещё нужно проверить тестовым сигналом.')
    if selected['virtual_hint']:
        warnings.append('Выбранный интерфейс похож на VPN/виртуальный. Проверьте выбор IP для аудио.')
    return {'station_ip': station_ip, 'audio_host': selected['address'], 'mode': mode,
            'route_ip': route_ip, 'same_subnet': selected['same_subnet'],
            'interface': selected['name'], 'prefix': selected['prefix'],
            'candidates': candidates, 'warnings': warnings}


def select_network(station_ip: str, override: str = '') -> dict:
    try:
        route_ip = local_ip_for(station_ip)
    except OSError:
        route_ip = ''
    result = choose_address(station_ip, interfaces(), route_ip, override)
    # Reject invented/non-local addresses rather than advertising a black hole.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((result['audio_host'], 0))
    except OSError as exc:
        raise BridgeError(f'IP {result["audio_host"]} не доступен на этом компьютере. '
                          'Выберите его фактический адрес Wi-Fi/Ethernet или верните автоматический выбор.') from exc
    return result
