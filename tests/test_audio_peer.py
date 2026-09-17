"""NAT regression: exact IP fixtures, mocked TCP peer for HTTP integration.

No live Windows, mDNS, Yandex login, physical Station or real NAT router is
used. Network tests use real loopback HTTP with a TEST-ONLY peer substitution.
"""
from __future__ import annotations
import base64
import json
from pathlib import Path
import shutil
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
import pytest

from station_bridge import access
from station_bridge.access import AudioPeerPolicy, private_peer, record_matches
from station_bridge.app import Controller, make_ui_app
from station_bridge.media import MediaServer
from station_bridge.protocol import BridgeError

PC, SPEAKER, RELAY = '10.0.0.156', '192.168.1.64', '10.0.0.1'
FP = 'ab' * 32
DEVICE = {'id': 'fixture-speaker', 'host': SPEAKER, 'platform': 'fixture', 'port': 1961}


def policy():
    p = AudioPeerPolicy()
    p.configure(PC, SPEAKER)
    return p


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def req(media, peer=RELAY, name='audio.wav', method='GET', token=None):
    return SimpleNamespace(remote=peer, method=method,
            match_info={'token': media.token if token is None else token, 'name': name})


@pytest.mark.parametrize('peer', ['', '127.0.0.2', '8.8.8.8', '0.0.0.0', '::1',
                                 '169.254.1.1', '224.0.0.1', '10.0.0.0/24', 'example.com'])
def test_no_public_special_hostname_or_subnet_approval(peer):
    with pytest.raises(BridgeError): private_peer(peer)
    p = policy()
    assert not p.observe(peer, 'capability')
    assert not p.pending('capability')


@pytest.mark.parametrize('peer', ['10.0.0.1', '172.16.5.1', '192.168.1.1'])
def test_private_address_alone_does_not_grant_access(peer):
    p = policy()
    assert not p.allows(peer)
    assert p.observe(peer, 'current-capability')
    assert not p.allows(peer)
    challenge = p.pending('current-capability', challenge=True)
    assert p.approve(peer, challenge['challenge'], 'current-capability') == peer
    assert p.allows(peer)
    assert not p.allows('10.1.2.3')
    assert p.allows(SPEAKER)


def test_reported_exact_topology_and_pending_nonce_is_not_audio_token():
    p = policy()
    assert p.observe(RELAY, 'secret-media-url-token')
    assert not p.observe(RELAY, 'secret-media-url-token')
    pending = p.pending('secret-media-url-token', challenge=True)
    assert pending['ip'] == RELAY
    assert pending['challenge'] != 'secret-media-url-token'
    assert 'resource' not in pending
    assert 'challenge' not in p.pending('secret-media-url-token')
    assert 'secret-media-url-token' not in json.dumps(pending)
    assert p.approve(RELAY, pending['challenge'], 'secret-media-url-token') == RELAY
    assert not p.pending('secret-media-url-token')


def test_second_peer_cannot_replace_live_candidate():
    p = policy(); p.observe(RELAY, 'token')
    first = p.pending('token', challenge=True)
    assert not p.observe('10.0.0.2', 'token')
    assert p.pending('token', challenge=True)['challenge'] == first['challenge']
    assert p.pending('token')['ip'] == RELAY


@pytest.mark.parametrize('peer,nonce,resource', [
    ('10.0.0.2', 'correct', 'token'), (RELAY, 'wrong', 'token'),
    (RELAY, '', 'token'), (RELAY, 'correct', 'new-token')])
def test_bad_stale_or_wrong_peer_approval_fails(peer, nonce, resource):
    p = policy(); p.observe(RELAY, 'token')
    challenge = p.pending('token', challenge=True)['challenge'] if nonce == 'correct' else nonce
    with pytest.raises(BridgeError): p.approve(peer, challenge, resource)
    assert not p.approved


def test_expired_challenge_cannot_be_used(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(access.time, 'monotonic', lambda: clock[0])
    p = policy(); p.observe(RELAY, 'token')
    pending = p.pending('token', challenge=True)
    clock[0] += 121
    with pytest.raises(BridgeError): p.approve(RELAY, pending['challenge'], 'token')
    assert p.observe(RELAY, 'token')
    assert p.pending('token', challenge=True)['challenge'] != pending['challenge']


def test_reconfigure_and_revoke_clear_permissions():
    p = policy(); p.restore(RELAY)
    p.configure(PC, '192.168.1.65')
    assert not p.allows(RELAY)
    p.restore(RELAY); p.revoke()
    assert not p.allows(RELAY)


@pytest.mark.parametrize('peer', [PC, SPEAKER])
def test_local_pc_and_speaker_cannot_be_relay(peer):
    p = policy()
    assert not p.observe(peer, 'token')
    with pytest.raises(BridgeError): p.restore(peer)


@pytest.mark.parametrize('change', ['host', 'pc', 'fingerprint', 'type'])
def test_saved_permission_is_bound_to_network_speaker_certificate(change):
    record = {'peer': RELAY, 'station_ip': SPEAKER, 'audio_host': PC, 'fingerprint': FP}
    assert record_matches(record, DEVICE, PC, FP)
    if change == 'host': assert not record_matches(record, {**DEVICE, 'host': '192.168.1.65'}, PC, FP)
    elif change == 'pc': assert not record_matches(record, DEVICE, '10.1.0.156', FP)
    elif change == 'fingerprint': assert not record_matches(record, DEVICE, PC, 'cd'*32)
    else: assert not record_matches([], DEVICE, PC, FP)


async def test_token_file_then_peer_check_and_scope_lifetime(tmp_path):
    logs = []
    m = MediaServer(tmp_path, logs.append)
    await m.bind('127.0.0.1', SPEAKER, port())
    try:
        await m.test_resource()
        for request in [req(m, token='wrong'), req(m, name='settings.json'), req(m, name='audio.mp3')]:
            with pytest.raises(web.HTTPNotFound): await m.handle(request)
            assert not m.peer_policy.pending(m.token)
        with pytest.raises(web.HTTPForbidden): await m.handle(req(m))
        assert m.requests == 0
        pending = m.peer_policy.pending(m.token, challenge=True)
        m.peer_policy.approve(RELAY, pending['challenge'], m.token)
        assert isinstance(await m.handle(req(m)), web.FileResponse)
        assert m.requests == 1
        assert isinstance(await m.handle(req(m, method='HEAD')), web.FileResponse)
        assert m.head_requests == 1 and m.requests == 1
        with pytest.raises(web.HTTPForbidden): await m.handle(req(m, peer='10.0.0.2'))
        with pytest.raises(web.HTTPNotFound): await m.handle(req(m, token='wrong'))
        old_token = m.token
        await m.stop()
        assert m.peer_policy.approved == RELAY  # permission survives stop, NOT rebind
        assert not m.peer_policy.pending(old_token)
        await m.test_resource()
        assert m.token != old_token
        with pytest.raises(web.HTTPNotFound): await m.handle(req(m, token=old_token))
        assert isinstance(await m.handle(req(m)), web.FileResponse)
        assert all(old_token not in msg and m.token not in msg for msg in logs)
        await m.close()
        assert not m.peer_policy.approved
    finally:
        await m.close()


async def test_controller_approves_resends_same_url_and_revokes(tmp_path):
    c = Controller(tmp_path/'settings')
    c.selected = dict(DEVICE)
    c.settings.data['pins'] = {DEVICE['id']: FP}
    c.glagol = SimpleNamespace(connected=True, send=AsyncMock(return_value={'acknowledged': True}))
    await c.media.bind('127.0.0.1', SPEAKER, port())
    try:
        await c.action('test', {})
        url, token = c.media.current_url(), c.media.token
        with pytest.raises(web.HTTPForbidden): await c.media.handle(req(c.media))
        pending = c.media.peer_policy.pending(token, challenge=True)
        result = await c.action('approve_audio_peer', pending)
        assert result['approved'] == RELAY and result['acknowledged']
        assert c.media.current_url() == url and c.media.token == token
        assert c.glagol.send.await_count == 2
        encoded = c.glagol.send.await_args.args[0]['data']
        assert url.encode() in base64.b64decode(encoded)
        saved = json.loads(c.settings.config_path.read_text())
        assert saved['audio_peer_approvals'][DEVICE['id']]['peer'] == RELAY
        assert token not in json.dumps(saved)
        assert pending['challenge'] not in json.dumps(saved)
        await c.action('revoke_audio_peer', {})
        assert not c.media.token and not c.media.peer_policy.approved
        assert not c.settings.data.get('active_audio_peer')
        assert DEVICE['id'] not in c.settings.data['audio_peer_approvals']
        with pytest.raises(BridgeError): await c.action('approve_audio_peer', pending)
    finally:
        await c.media.close(); shutil.rmtree(c.cache, ignore_errors=True)


async def test_controller_restores_only_matching_scope(tmp_path):
    c = Controller(tmp_path/'settings')
    c.media.peer_policy.configure(PC, SPEAKER)
    record = {'peer': RELAY, 'station_ip': SPEAKER, 'audio_host': PC, 'fingerprint': FP}
    c.settings.data['audio_peer_approvals'] = {DEVICE['id']: record}
    try:
        c.restore_audio_peer(DEVICE, PC, FP)
        assert c.media.peer_policy.approved == RELAY
        c.media.peer_policy.configure(PC, SPEAKER)
        c.restore_audio_peer(DEVICE, PC, 'cd'*32)
        assert not c.media.peer_policy.approved
        assert not c.settings.data.get('active_audio_peer')
        c.restore_audio_peer({**DEVICE, 'id': 'other'}, PC, FP)
        assert not c.media.peer_policy.approved
    finally:
        shutil.rmtree(c.cache, ignore_errors=True)


async def test_http_roundtrip_403_to_200_and_206_without_opening_other_peers(tmp_path):
    c = Controller(tmp_path/'settings')
    c.selected = dict(DEVICE)
    c.settings.data['pins'] = {DEVICE['id']: FP}
    c.glagol = SimpleNamespace(connected=True, send=AsyncMock(return_value={'acknowledged': True}),
                              safe_state=lambda: {'connected': True, 'playing': False})
    m = c.media
    real_handle = m.handle
    peer = [RELAY]
    async def simulated_nat(request):
        # This is TEST-ONLY transport substitution, not a feature of the app.
        return await real_handle(request.clone(remote=peer[0]))
    m.handle = simulated_nat
    await m.bind('127.0.0.1', SPEAKER, port())
    ui_port = port()
    runner = web.AppRunner(make_ui_app(c, 'test-ui-key', ui_port))
    await runner.setup(); await web.TCPSite(runner, '127.0.0.1', ui_port).start()
    base = f'http://127.0.0.1:{ui_port}'
    try:
        await c.action('test', {})
        url = m.current_url()
        async with aiohttp.ClientSession() as client:
            # Exactly six failed requests, like the supplied diagnostic.
            for _ in range(6):
                async with client.get(url) as r:
                    assert r.status == 403 and b'RIFF' not in await r.read()
            assert m.incoming_requests == m.rejected_requests == 6
            assert m.requests == 0 and m.last_rejection == 'peer_not_allowed'
            pending = c.snapshot()['peer_approval']
            payload = {'command': 'approve_audio_peer', **pending}
            for headers, expected in [({}, 401), ({'X-Bridge-Key': 'wrong'}, 401),
                ({'X-Bridge-Key': 'test-ui-key', 'Origin': 'https://evil.test'}, 403)]:
                async with client.post(base+'/api/action', headers=headers, json=payload) as r:
                    assert r.status == expected
            assert not m.peer_policy.approved
            async with client.get(base+'/api/diagnostic', headers={'X-Bridge-Key':'test-ui-key'}) as r:
                text = await r.text(); report = json.loads(text)
                assert pending['challenge'] not in text and m.token not in text
                assert report['media']['pending_audio_peer']['ip'] == RELAY
                assert 'peer_approval' not in report
            async with client.post(base+'/api/action', headers={'X-Bridge-Key': 'test-ui-key'}, json=payload) as r:
                assert r.status == 200, await r.text()
                assert (await r.json())['approved'] == RELAY
            async with client.head(url) as r:
                assert r.status == 200 and int(r.headers['Content-Length']) > 0
            async with client.get(url) as r:
                assert r.status == 200
                data = await r.read(); assert data.startswith(b'RIFF')
                assert int(r.headers['Content-Length']) == len(data)
            async with client.get(url, headers={'Range': 'bytes=0-31'}) as r:
                assert r.status == 206 and await r.read() == data[:32]
                assert r.headers['Content-Range'] == f'bytes 0-31/{len(data)}'
            peer[0] = '10.0.0.2'
            async with client.get(url, headers={'X-Forwarded-For': RELAY}) as r:
                assert r.status == 403
            peer[0] = RELAY
            async with client.get(url.replace(m.token, 'invalid')) as r:
                assert r.status == 404
            # HLS access shares the same policy; real encoding tested elsewhere.
            await m.new_resource('live', 'Fixture HLS')
            m.resource_name = 'live.m3u8'
            (m.directory/'live.m3u8').write_text('#EXTM3U\n#EXTINF:1.0,\nseg000001.ts\n')
            (m.directory/'seg000001.ts').write_bytes(b'G'*188)
            async with client.get(m.current_url()) as r:
                assert r.status == 200 and '#EXTM3U' in await r.text()
            async with client.get(m.url('seg000001.ts')) as r:
                assert r.status == 200 and len(await r.read()) == 188
                assert m.segment_requests == 1
    finally:
        await runner.cleanup(); await m.close(); shutil.rmtree(c.cache, ignore_errors=True)


def test_ui_has_approval_and_revoke_controls():
    root = Path(__file__).resolve().parents[1]
    html = (root/'web/index.html').read_text()
    js = (root/'web/app.js').read_text()
    for ident in ['peerPanel', 'peerApprove', 'peerRevoke', 'peerDescription', 'peerAllowed', 'peerAllowedText']:
        assert f'id="{ident}"' in html
    assert "command('approve_audio_peer'" in js
    assert 'challenge:candidate.challenge' in js
    assert "command('revoke_audio_peer'" in js
