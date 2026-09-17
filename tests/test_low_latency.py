"""Native encoder/HTTP tests; no claim of Windows or speaker hardware testing."""
import array
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
import aiohttp
from aiohttp import web
import pytest
from station_bridge.audio import HLSCapture
from station_bridge.app import Controller
from station_bridge.live_stream import FRAME_BYTES, FRAME_SECONDS, FrameRing, MP3FrameParser
from station_bridge.media import MediaServer
from station_bridge.protocol import BridgeError
from test_servers import free_port
from test_live_capture_fix import FakePA, fake_backend


def fake_frame(value=0):
    # Only used for ring/parser unit tests, never for codec/audio tests.
    return b'\xff\xfb\xe4\x64\x00\x00' + bytes([value]) * (FRAME_BYTES - 6)


@pytest.mark.parametrize('size', [1, 2, 4, 37, 959, 960, 963, 3840])
def test_mp3_parser_arbitrary_pipe_boundaries(size):
    parser = MP3FrameParser()
    frames = [fake_frame(i) for i in range(7)]
    raw = b''.join(frames)
    got = []
    for offset in range(0, len(raw), size):
        got.extend(parser.feed(raw[offset:offset+size]))
    assert got == frames and not parser.pending


@pytest.mark.parametrize('offset,value', [(0,0), (1,0), (2,0), (4,1), (5,128)])
def test_mp3_parser_rejects_other_formats_and_reservoir(offset,value):
    frame = bytearray(fake_frame()); frame[offset] = value
    with pytest.raises(ValueError): MP3FrameParser().feed(frame)


def test_ring_is_bounded_and_slow_reader_reports_skips():
    ring = FrameRing(8)
    for i in range(1000): ring.append(fake_frame(i % 256))
    assert ring.snapshot()['retained_bytes'] == 8 * FRAME_BYTES
    got = ring.read(0, 2)
    assert got.skipped == 992 and got.cursor == 994 and got.newest == 1000
    assert len(got.data) == 2 * FRAME_BYTES
    assert ring.live_cursor(6) == 994


@pytest.mark.parametrize('capacity', [0, 7, 2049])
def test_invalid_ring_capacity(capacity):
    with pytest.raises(ValueError): FrameRing(capacity)


def test_incomplete_frame_not_published():
    ring = FrameRing()
    with pytest.raises(ValueError): ring.append(b'bad')
    assert ring.snapshot()['encoded_frames'] == 0


def test_stale_pcm_is_discarded_and_recent_pcm_survives(tmp_path):
    c = HLSCapture(tmp_path, synthetic=True)
    size = c.frames * c.channels * 2
    c.queue.put_nowait((time.monotonic()-5, b'\x01\x01' * (size//2)))
    c.queue.put_nowait((time.monotonic(), b'\x02\x02' * (size//2)))
    assert c._take_pcm(bytearray(), size, time.monotonic()) == b'\x02\x02' * (size//2)
    assert c.stale_frames == c.frames and c.dropped == 1
    assert c.silence_frames == 0


def test_short_pcm_is_padded_without_replaying_old_data(tmp_path):
    c = HLSCapture(tmp_path, synthetic=True)
    c.queue.put_nowait((time.monotonic(), bytes([3])*8))
    assert c._take_pcm(bytearray(), 16, time.monotonic()) == bytes([3])*8 + bytes(8)
    assert c.silence_frames == 2


@pytest.mark.parametrize('transport', ['invalid', 'MP3', 'hls+mp3'])
def test_invalid_transport_rejected_before_capture(tmp_path,transport):
    with pytest.raises(BridgeError): HLSCapture(tmp_path,transport=transport)


def test_real_mp3_encode_independent_reconnect_decode_and_stop(tmp_path):
    c = HLSCapture(tmp_path/'mp3', synthetic=True, transport='mp3')
    c.start()
    try:
        end = time.monotonic() + 4
        while not c.ready() and time.monotonic() < end: time.sleep(.01)
        assert c.ready(), list(c.stderr)
        time.sleep(.5)
        # Start decoder at a middle frame, not the beginning of the encoder stream.
        data = c.mp3.read(8, 100).data
        decoded = subprocess.run([shutil.which('ffmpeg'),'-v','error','-f','mp3','-i','pipe:0',
                                  '-f','s16le','pipe:1'],input=data,capture_output=True,timeout=10)
        assert decoded.returncode == 0 and decoded.stdout
        samples = array.array('h', decoded.stdout)
        assert max(map(abs,samples)) > 1500
        assert c.stats()['first_encoded_frame_seconds'] < 2
        assert c.stats()['encoder_backlog_seconds'] < .5
        assert c.mp3.snapshot()['retained_bytes'] <= 128 * FRAME_BYTES
        assert not list(c.directory.iterdir()), 'MP3 must not save audio files'
    finally: c.stop()
    assert not c.mp3_thread.is_alive() and not c.writer.is_alive()
    assert c.process.poll() is not None


async def mp3_server(tmp_path):
    m = MediaServer(tmp_path, lambda text: None)
    await m.bind('127.0.0.1','127.0.0.1',free_port())
    url = await m.start_live(None, synthetic=True, transport='mp3')
    return m,url


async def test_progressive_http_exact_length_no_chunking_and_head(tmp_path):
    m,url = await mp3_server(tmp_path)
    m.response_frames = 20  # finite 0.48-second response for exact byte accounting
    try:
        assert m.current_url() == url and url.endswith('live.mp3')
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.head(url) as r:
                assert r.status == 200
                assert r.headers['Content-Length'] == str(20*FRAME_BYTES)
                assert not await r.read()
                assert m.live_connections == 0
            # A live endpoint cannot seek. RFC allows ignoring Range with 200.
            async with s.get(url,headers={'Range':'bytes=0-'}) as r:
                assert r.status == 200 and r.headers['Accept-Ranges']=='none'
                assert 'Transfer-Encoding' not in r.headers
                data=await r.read()
                assert len(data)==20*FRAME_BYTES
                assert len(MP3FrameParser().feed(data))==20
        assert m.live_bytes_sent == len(data)
        assert m.first_audio_request_seconds is not None
        assert m.segment_requests == 0 and m.live_connections == 1
        assert 'не измерение' in m.stats()['latency_note']
    finally: await m.close()


async def test_http_disconnect_does_not_block_producer_and_reconnect_uses_recent_frames(tmp_path):
    m,url = await mp3_server(tmp_path)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:
                assert len(await r.content.readexactly(1920)) == 1920
            first = m.capture.mp3.snapshot()['encoded_frames']
            await asyncio.sleep(.4)
            assert m.capture.mp3.snapshot()['encoded_frames'] > first + 5
            async with s.get(url) as r:
                assert r.status==200
                await r.content.readexactly(5760)
            assert m.live_connections == 2
            assert m.live_send_lag_ms <= 144
    finally: await m.close()
    assert not m._live_tasks


async def test_stop_revokes_url_and_aborts_all_active_readers(tmp_path):
    m,url = await mp3_server(tmp_path)
    async with aiohttp.ClientSession() as s:
        r=await s.get(url)
        await r.content.readexactly(FRAME_BYTES)
        capture=m.capture
        await asyncio.wait_for(m.stop(),3)
        assert not m._live_tasks and not capture.mp3_thread.is_alive()
        with pytest.raises(aiohttp.ClientPayloadError): await r.read()
        async with s.get(url) as old: assert old.status==404
        r.close()
    await m.close()


async def test_live_mp3_is_not_exposed_without_valid_token_and_approved_peer(tmp_path):
    m,url = await mp3_server(tmp_path)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url.replace(m.token,'wrong')) as r: assert r.status==404
        class Request:
            remote = '192.168.44.99'
            match_info = {'token':m.token,'name':'live.mp3'}
            method='GET'
        with pytest.raises(web.HTTPForbidden): await m.handle(Request())
        assert m.peer_policy.pending(m.token)['ip']=='192.168.44.99'
        assert m.live_connections == 0
    finally: await m.close()


async def test_client_limit(tmp_path):
    m,url=await mp3_server(tmp_path)
    try:
        async with aiohttp.ClientSession() as s:
            a=await s.get(url); b=await s.get(url)
            await a.content.readexactly(FRAME_BYTES); await b.content.readexactly(FRAME_BYTES)
            async with s.get(url) as c: assert c.status==503
            a.close(); b.close()
    finally: await m.close()


async def test_no_explicit_mp3_selection_keeps_hls_backwards_compatible(tmp_path):
    m=MediaServer(tmp_path,lambda text:None)
    await m.bind('127.0.0.1','127.0.0.1',free_port())
    try:
        url=await m.start_live(None,synthetic=True)
        assert url.endswith('m3u8') and m.transport=='hls'
    finally:await m.close()


async def test_resync_restarts_active_profile_but_not_stopped_or_other_source(tmp_path,monkeypatch):
    c=Controller(tmp_path/'settings')
    c.glagol=SimpleNamespace(connected=True)
    called=[]
    async def restart(data,synthetic=False):
        called.append((dict(data),synthetic));return {}
    monkeypatch.setattr(c,'start_live_playback',restart)
    try:
        with pytest.raises(BridgeError): await c.action('resync',{})
        c.media.kind='live'
        c.live_options={'transport':'mp3','synthetic':False,'device_index':16,'device_key':'known'}
        await c.action('resync',{})
        assert called[0][0]['transport']=='mp3' and called[0][0]['device_index']==16
        with pytest.raises(BridgeError): await c.action('resync',{})
        assert len(called)==1
    finally: shutil.rmtree(c.cache,ignore_errors=True)


async def test_nat_repeat_uses_mp3_directive_not_hls(tmp_path,monkeypatch):
    # Direct transport must not accidentally retain HLS=True from version 1.0.4.
    c=Controller(tmp_path/'settings')
    await c.start()
    await c.media.bind('127.0.0.1','192.168.1.64',free_port())
    await c.media.start_live(None,synthetic=True,transport='mp3')
    c.media.peer_policy.observe('10.0.0.1',c.media.token)
    pending=c.media.peer_policy.pending(c.media.token,challenge=True)
    called=[]
    async def send(url,title,hls=False): called.append(hls); return {'acknowledged':True}
    monkeypatch.setattr(c,'send_audio',send)
    try:
        await c.approve_audio_peer({'ip':pending['ip'],'challenge':pending['challenge']})
        assert called==[False]
    finally:await c.close()


def test_queue_overflow_with_fake_windows_callback_is_bounded(tmp_path,monkeypatch):
    import station_bridge.audio as audio
    p=FakePA(modes={21:'tone'})
    monkeypatch.setattr(audio,'backend',lambda:fake_backend(p))
    c=HLSCapture(tmp_path/'capture',21,transport='mp3')
    c.start()
    try:
        time.sleep(.6)
        assert c.stats()['running'] and c.stats()['input_level']>.05
        assert c.queue.qsize()<=8 and c.mp3.snapshot()['retained_bytes']<=128*FRAME_BYTES
    finally:c.stop()
    assert p.terminated
