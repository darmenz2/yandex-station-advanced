"""Regression tests for LAN callback selection and honest HTTP diagnostics.

Topologies are simulated. These are NOT real Windows/VPN/Midi acceptance tests.
"""
import asyncio
import json
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
import pytest
from station_bridge import network
from station_bridge.app import Controller, make_ui_app
from station_bridge.media import MediaServer
from station_bridge.protocol import BridgeError


def nic(address, prefix=24, name='Ethernet', virtual=False):
    return {'address': address, 'prefix': prefix, 'name': name, 'virtual_hint': virtual}


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_reported_vpn_topology_prefers_real_lan():
    # 192.168.1.100 is a fixture, NOT a proposed address for the user.
    result = network.choose_address('192.168.1.64', [
        nic('10.0.0.156', 32, 'WireGuard', True), nic('192.168.1.100')], '10.0.0.156')
    assert result['audio_host'] == '192.168.1.100'
    assert result['mode'] == 'same_subnet'
    assert result['route_ip'] == '10.0.0.156'
    assert result['same_subnet']
    assert result['warnings']


def test_tunnel_address_not_inferred_from_10_prefix_alone():
    result = network.choose_address('10.0.0.64', [nic('10.0.0.156')], '10.0.0.156')
    assert result['audio_host'] == '10.0.0.156'
    assert result['same_subnet']
    assert not result['warnings']


def test_routed_network_is_allowed_but_not_claimed_reachable():
    result = network.choose_address('192.168.1.64', [nic('10.0.0.156')], '10.0.0.156')
    assert result['audio_host'] == '10.0.0.156'
    assert result['mode'] == 'route_fallback'
    assert not result['same_subnet']
    assert 'Маршрутизация' in ' '.join(result['warnings'])


def test_manual_override_can_select_routed_private_address():
    result = network.choose_address('192.168.1.64', [nic('192.168.1.100'), nic('10.0.0.156')],
                                    '192.168.1.100', '10.0.0.156')
    assert result['audio_host'] == '10.0.0.156'
    assert result['mode'] == 'manual'


@pytest.mark.parametrize('address', ['192.168.1.64', '0.0.0.0', '8.8.8.8', '127.0.0.1', 'example.test'])
def test_manual_invalid_or_speaker_ip_rejected(address):
    with pytest.raises(BridgeError):
        network.choose_address('192.168.1.64', [], '', address)


@pytest.mark.parametrize('pc,prefix,speaker,expected', [
    ('192.168.0.10', 23, '192.168.1.64', True),
    ('192.168.1.100', 25, '192.168.1.200', False),
    ('192.168.1.100', None, '192.168.1.64', False),
    ('192.168.1.100', 32, '192.168.1.64', False),
    ('172.20.10.2', 20, '172.20.15.7', True),
])
def test_real_netmask_not_assumed_24(pc, prefix, speaker, expected):
    assert network.on_link(pc, prefix, speaker) is expected


def test_multiple_lan_interfaces_preserve_os_route_choice():
    result = network.choose_address('192.168.1.64', [nic('192.168.1.90'), nic('192.168.1.100')],
                                    '192.168.1.100')
    assert result['audio_host'] == '192.168.1.100'


def test_physical_same_subnet_beats_virtual_matching_route():
    result = network.choose_address('192.168.1.64', [nic('192.168.1.10', 24, 'VPN', True),
                                                   nic('192.168.1.100')], '192.168.1.10')
    assert result['audio_host'] == '192.168.1.100'


def test_no_route_and_no_interface_has_clear_error():
    with pytest.raises(BridgeError, match='Укажите'):
        network.choose_address('192.168.1.64', [], '')


def test_ipv6_public_and_malformed_ifaddr_entries_filtered(monkeypatch):
    fake = SimpleNamespace(get_adapters=lambda: [SimpleNamespace(name='nic', nice_name='Wi-Fi', ips=[
        SimpleNamespace(ip='192.168.1.100', network_prefix=23),
        SimpleNamespace(ip=('::1', 0, 0), network_prefix=128),
        SimpleNamespace(ip='8.8.8.8', network_prefix=24),
        SimpleNamespace(ip='127.0.0.1', network_prefix=8),
        SimpleNamespace(ip='192.168.1.200', network_prefix=70)])])
    monkeypatch.setitem(sys.modules, 'ifaddr', fake)
    rows = network.interfaces()
    assert rows == [nic('192.168.1.100', 23, 'Wi-Fi')]


def test_interface_names_can_hint_virtual_without_changing_10_network(monkeypatch):
    fake = SimpleNamespace(get_adapters=lambda: [SimpleNamespace(name='nic', nice_name='WireGuard Tunnel',
                ips=[SimpleNamespace(ip='10.0.0.156', network_prefix=32)])])
    monkeypatch.setitem(sys.modules, 'ifaddr', fake)
    assert network.interfaces()[0]['virtual_hint']


def test_interface_fallback_keeps_unknown_mask(monkeypatch):
    monkeypatch.setitem(sys.modules, 'ifaddr', None)
    monkeypatch.setattr(network.socket, 'getaddrinfo', lambda *args: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.100', 0))])
    assert network.interfaces()[0]['prefix'] is None


def test_manual_address_must_actually_bind_on_pc(monkeypatch):
    monkeypatch.setattr(network, 'local_ip_for', lambda host: '192.168.1.100')
    monkeypatch.setattr(network, 'interfaces', lambda: [nic('192.168.1.100')])
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def bind(self, target): raise OSError('not assigned')
    monkeypatch.setattr(network.socket, 'socket', lambda *a: Socket())
    with pytest.raises(BridgeError, match='не доступен'):
        network.select_network('192.168.1.64', '192.168.1.100')


def test_auto_address_is_bound_checked(monkeypatch):
    monkeypatch.setattr(network, 'local_ip_for', lambda host: '10.0.0.156')
    monkeypatch.setattr(network, 'interfaces', lambda: [nic('192.168.1.100'), nic('10.0.0.156', 32, 'VPN', True)])
    bound = []
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def bind(self, target): bound.append(target)
    monkeypatch.setattr(network.socket, 'socket', lambda *a: Socket())
    assert network.select_network('192.168.1.64')['audio_host'] == '192.168.1.100'
    assert bound == [('192.168.1.100', 0)]


async def test_health_probe_does_not_claim_station_download(tmp_path):
    media = MediaServer(tmp_path, lambda text: None)
    await media.bind('127.0.0.1', '127.0.0.1', free_port())
    try:
        await media.test_resource()
        assert await media.self_check()
        assert media.requests == 0
        assert media.incoming_requests == 0
        assert media.segment_requests == 0
    finally:
        await media.close()


async def test_head_is_counted_separately_and_invalid_url_is_visible(tmp_path):
    media = MediaServer(tmp_path, lambda text: None)
    await media.bind('127.0.0.1', '127.0.0.1', free_port())
    try:
        url = await media.test_resource()
        async with aiohttp.ClientSession() as session:
            async with session.head(url) as response:
                assert response.status == 200
                assert int(response.headers['Content-Length']) > 0
            assert media.head_requests == 1 and media.requests == 0
            async with session.get(f'http://127.0.0.1:{media.port}/m/wrong/audio.wav') as response:
                assert response.status == 404
            assert media.incoming_requests == 2
            assert media.rejected_requests == 1
            assert media.last_rejection == 'missing_or_expired_resource'
            assert media.last_peer == '127.0.0.1'
            assert media.token not in json.dumps(media.stats())
            async with session.get(url) as response:
                assert response.status == 200
                assert (await response.read())[:4] == b'RIFF'
            assert media.requests == 1
    finally:
        await media.close()


async def test_unselected_peer_rejection_visible_without_exposing_file(tmp_path):
    media = MediaServer(tmp_path, lambda text: None)
    await media.bind('127.0.0.1', '192.168.1.64', free_port())
    try:
        url = await media.test_resource()
        # 127.0.0.2 is a different TCP peer; do not trust spoofed proxy headers.
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(local_addr=('127.0.0.2', 0))) as session:
            async with session.get(url, headers={'X-Forwarded-For': '192.168.1.64'}) as response:
                assert response.status == 403
                assert b'RIFF' not in await response.read()
        assert media.incoming_requests == 1
        assert media.rejected_requests == 1
        assert media.last_rejection == 'peer_not_allowed'
        assert media.requests == 0
    finally:
        await media.close()


async def test_controller_connect_uses_selected_interface_and_persists(tmp_path, monkeypatch):
    from station_bridge import app as module
    controller = Controller(tmp_path / 'settings')
    try:
        controller.auth = SimpleNamespace(logged_in=True, device_token=AsyncMock(return_value='SECRET'))
        controller.glagol = SimpleNamespace(connect=AsyncMock())
        controller.media = SimpleNamespace(bind=AsyncMock(), self_check=AsyncMock(return_value=True), port=8808)
        controller.disconnect = AsyncMock()
        controller.persist_account = AsyncMock()
        monkeypatch.setattr(module, 'get_fingerprint', AsyncMock(return_value='ab' * 32))
        selection = network.choose_address('192.168.1.64', [nic('192.168.1.100'),
                        nic('10.0.0.156', 32, 'VPN', True)], '10.0.0.156')
        received = []
        def select(host, override):
            received.append((host, override)); return selection
        monkeypatch.setattr(module, 'select_network', select)
        monkeypatch.setattr(module, 'interfaces', lambda: [nic('192.168.1.100')])
        controller.settings.data['pins'] = {'speaker-fixture': 'ab' * 32}
        device = {'id': 'speaker-fixture', 'platform': 'fixture', 'host': '192.168.1.64', 'port': 1961}
        assert await controller.connect(device, audio_host='') == {'connected': True}
        controller.media.bind.assert_awaited_once_with('192.168.1.100', '192.168.1.64')
        assert received == [('192.168.1.64', '')]
        saved = json.loads(controller.settings.config_path.read_text())
        assert saved['audio_host'] == '192.168.1.100' and saved['audio_port'] == 8808
        assert 'SECRET' not in json.dumps(saved)
        assert controller.network['local_http_check'] is True
    finally:
        import shutil
        shutil.rmtree(controller.cache, ignore_errors=True)


async def test_diagnostics_includes_routing_without_tokens(tmp_path, monkeypatch):
    controller = Controller(tmp_path / 'settings')
    await controller.start()
    controller.network = {'audio_host': '192.168.1.100', 'station_ip': '192.168.1.64',
                          'route_ip': '10.0.0.156', 'local_http_check': True}
    controller.last_audio_command = {'directive': 'audio_play', 'acknowledged': True}
    controller.auth.x_token = 'PRIVATE_ACCOUNT_TOKEN'
    controller.glagol.token = 'PRIVATE_STATION_TOKEN'
    controller.media.token = 'PRIVATE_AUDIO_URL_TOKEN'
    port = free_port()
    runner = web.AppRunner(make_ui_app(controller, 'PRIVATE_UI_KEY', port))
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', port).start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'http://127.0.0.1:{port}/api/diagnostic',
                                   headers={'X-Bridge-Key': 'PRIVATE_UI_KEY'}) as response:
                assert response.status == 200
                report = await response.json()
        raw = json.dumps(report)
        from station_bridge import __version__
        assert report['version'] == __version__
        assert report['network']['route_ip'] == '10.0.0.156'
        assert report['network']['audio_host'] == '192.168.1.100'
        assert report['last_audio_command']['acknowledged']
        assert 'PRIVATE_' not in raw
        assert 'head_requests' in report['media']
    finally:
        await runner.cleanup()
        await controller.close()


async def test_repeated_test_does_not_revoke_pending_audio_url(tmp_path):
    controller = Controller(tmp_path / 'settings')
    controller.glagol = SimpleNamespace(connected=True, send=AsyncMock(return_value={'acknowledged': True}))
    await controller.media.bind('127.0.0.1', '127.0.0.1', free_port())
    try:
        first = await controller.action('test', {})
        url = controller.media.url('audio.wav')
        token = controller.media.token
        second = await controller.action('test', {})
        assert first['acknowledged'] and second['pending']
        assert controller.media.token == token
        controller.glagol.send.assert_awaited_once()
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                assert response.status == 200
                assert (await response.read())[:4] == b'RIFF'
    finally:
        await controller.media.close()
        import shutil
        shutil.rmtree(controller.cache, ignore_errors=True)


def test_network_ui_controls_exist_and_javascript_is_parseable():
    from pathlib import Path
    import re
    import shutil
    import subprocess
    root = Path(__file__).resolve().parents[1]
    html = (root / 'web/index.html').read_text()
    js = (root / 'web/app.js').read_text()
    controls = {'audioHost', 'manualAudioHost', 'manualAudioHostLabel', 'refreshNetwork',
                'networkHint', 'networkStatus'}
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    assert controls.issubset(set(ids))
    assert "audio_host:audioHostValue()" in js
    assert "state.trust.audio_host" in js
    if shutil.which('node'):
        run = subprocess.run(['node', '--check', str(root / 'web/app.js')], capture_output=True)
        assert run.returncode == 0, run.stderr


def test_firewall_helper_requires_confirmation_and_exact_pc_speaker():
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1] / 'installer/Firewall.ps1').read_text(encoding='utf-8-sig')
    assert 'MessageBox' in script and "'YesNo'" in script
    assert '-LocalAddress $r.hostIp -LocalPort $r.port -RemoteAddress $r.remote' in script
    assert '$settings.audio_peer_approvals' in script
    assert '[string]$r.fingerprint -eq $pin' in script
    assert '-Profile Private -Program $python' in script
    assert 'Set-NetFirewallProfile' not in script
    assert 'Set-NetConnectionProfile' not in script
    assert 'New-NetRoute' not in script
