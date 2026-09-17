"""PCM/fast regressions. Real HTTP/codec tests; Windows capture is simulated."""
from __future__ import annotations
import array
import asyncio
import contextlib
import io
import struct
import time
import wave
from types import SimpleNamespace
import aiohttp
from aiohttp import web
import pytest
from station_bridge.audio import HLSCapture
from station_bridge import audio, realtime
from station_bridge.live_stream import FrameRing
from station_bridge.media import MediaServer
from station_bridge.pcm_stream import wav_header, pcm_response_blocks
from station_bridge.protocol import BridgeError
from station_bridge.app import Controller
from test_servers import free_port
from test_live_capture_fix import FakePA, device, fake_backend, tone

@pytest.mark.parametrize('rate',[8000,16000,44100,48000,96000,192000])
@pytest.mark.parametrize('channels',[1,2])
def test_wav_header_and_riff_budget(rate,channels):
    frames=max(128,rate//100)
    budget=pcm_response_blocks(rate,channels,frames)
    header=wav_header(rate,channels,budget*frames*channels*2)
    assert len(header)==44
    assert struct.unpack_from('<I',header,4)[0]==36+budget*frames*channels*2
    with wave.open(io.BytesIO(header),'rb') as w:
        assert (w.getframerate(),w.getnchannels(),w.getsampwidth())==(rate,channels,2)
        assert w.getnframes()==budget*frames

@pytest.mark.parametrize('rate,channels,size',[(0,2,0),(48000,6,0),(48000,2,1),(48000,2,2**32),(48000,2,-4)])
def test_invalid_wav_formats_rejected(rate,channels,size):
    with pytest.raises(ValueError):wav_header(rate,channels,size)

@pytest.mark.parametrize('profile,block_ms,preroll', [('fast',10,1),('balanced',20,3)])
def test_pcm_has_no_ffmpeg_no_files_and_stops(tmp_path,monkeypatch,profile,block_ms,preroll):
    def disallow():raise AssertionError('PCM must not require FFmpeg')
    monkeypatch.setattr(audio,'ffmpeg_executable',disallow)
    c=HLSCapture(tmp_path/'pcm',synthetic=True,transport='pcm',latency_profile=profile)
    c.start()
    try:
        end=time.monotonic()+2
        while not c.ready() and time.monotonic()<end:time.sleep(.005)
        assert c.ready() and c.process is None
        assert c.stats()['encoder_bypassed'] and c.stats()['running']
        assert c.stats()['requested_block_ms']==block_ms
        assert c.preroll_frames==preroll
        data=c.pcm_ring.read(c.pcm_ring.live_cursor(1),1).data
        assert max(map(abs,array.array('h',data)))>2000
        assert not list(c.directory.iterdir())
        assert c.stats()['encoder_active'] is False
    finally:c.stop()
    assert not c.writer.is_alive() and not c.stats()['running']

@pytest.mark.parametrize('rate,channels',[(44100,1),(48000,2),(96000,2)])
def test_simulated_wasapi_native_pcm_format(tmp_path,monkeypatch,rate,channels):
    p=FakePA(rows=[device(21,'Direct [Loopback]',rate=rate,channels=channels)],modes={21:'tone'})
    monkeypatch.setattr(audio,'backend',lambda:fake_backend(p))
    c=HLSCapture(tmp_path,21,transport='pcm',latency_profile='fast')
    c.start()
    try:
        time.sleep(.15)
        assert c.stats()['running'] and c.input_meter.snapshot()['received_frames']>0
        assert (c.rate,c.channels)==(rate,channels)
        assert len(c.pcm_ring.read(0,1).data)==c.frames*channels*2
    finally:c.stop()
    assert p.terminated and all(s.closed for s in p.streams)

def test_multichannel_pcm_rejected_before_capture_start(tmp_path,monkeypatch):
    p=FakePA(rows=[device(21,'Surround [Loopback]',channels=8)])
    monkeypatch.setattr(audio,'backend',lambda:fake_backend(p))
    c=HLSCapture(tmp_path,21,transport='pcm')
    with pytest.raises(BridgeError,match='многоканального PCM'):c.start()
    assert not p.streams and p.terminated

async def pcm_server(tmp_path,profile='fast'):
    m=MediaServer(tmp_path,lambda _:None)
    await m.bind('127.0.0.1','127.0.0.1',free_port())
    url=await m.start_live(None,transport='pcm',synthetic=True,latency_profile=profile)
    return m,url

async def test_pcm_http_wav_exact_length_head_and_range(tmp_path):
    m,url=await pcm_server(tmp_path);m.pcm_response_blocks=20
    try:
        assert url.endswith('/live.wav') and m.current_url()==url
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.head(url) as r:
                assert r.status==200 and int(r.headers['Content-Length'])==44+20*1920
                assert not await r.read() and m.live_connections==0
            async with s.get(url,headers={'Range':'bytes=0-'}) as r:
                assert r.status==200 and 'Transfer-Encoding' not in r.headers
                assert r.headers['Content-Type']=='audio/wav' and r.headers['Accept-Ranges']=='none'
                raw=await r.read();assert len(raw)==int(r.headers['Content-Length'])
            with wave.open(io.BytesIO(raw),'rb') as w:
                assert w.getnframes()==20*480
                samples=array.array('h',w.readframes(w.getnframes()))
                assert max(map(abs,samples))>2000
        assert m.live_bytes_sent==len(raw)-44 and m.live_connections==1
        assert m.segment_requests==0 and m.capture.process is None
        assert m.live_send_lag_ms<=30 and m.stats()['live_write_queue_bytes']>=0
    finally:await m.close()

async def test_pcm_access_control_matches_mp3(tmp_path):
    m,url=await pcm_server(tmp_path)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url.replace(m.token,'bad')) as r:assert r.status==404
        request=SimpleNamespace(remote='10.0.0.1',method='GET',match_info={'token':m.token,'name':'live.wav'})
        with pytest.raises(web.HTTPForbidden):await m.handle(request)
        assert m.peer_policy.pending(m.token)['ip']=='10.0.0.1'
        assert m.live_connections==0
    finally:await m.close()

async def test_pcm_reconnect_fresh_header_and_revocation(tmp_path):
    m,url=await pcm_server(tmp_path)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:assert (await r.content.readexactly(44))[:4]==b'RIFF'
            first=m.capture.pcm_ring.snapshot()['encoded_frames']
            await asyncio.sleep(.15)
            assert m.capture.pcm_ring.snapshot()['encoded_frames']>first
            r=await s.get(url);assert (await r.content.readexactly(44))[:4]==b'RIFF'
            capture=m.capture
            await asyncio.wait_for(m.stop(),3)
            with pytest.raises(aiohttp.ClientPayloadError):await r.read()
            assert not m._live_tasks and not capture.writer.is_alive()
            async with s.get(url) as old:assert old.status==404
            r.close()
    finally:await m.close()

async def test_pcm_limits_simultaneous_readers(tmp_path):
    m,url=await pcm_server(tmp_path)
    try:
        async with aiohttp.ClientSession() as s:
            a=await s.get(url);b=await s.get(url)
            await a.content.readexactly(44);await b.content.readexactly(44)
            async with s.get(url) as r:assert r.status==503
            a.close();b.close()
    finally:await m.close()

def test_fast_profile_and_residual_pcm_expiry(tmp_path):
    c=HLSCapture(tmp_path,synthetic=True,transport='pcm',latency_profile='fast')
    size=c.frames*c.channels*2; buf=bytearray()
    c.queue.put((time.monotonic(),bytes([1])*size*2))
    assert c._take_pcm(buf,size,time.monotonic())==bytes([1])*size
    c._buffer_received_at=time.monotonic()-1
    assert c._take_pcm(buf,size,time.monotonic())==bytes(size)
    assert c.stale_frames==c.frames
    assert c.max_pcm_age==.06 and c.start_buffer_seconds==.02

def test_hls_ignores_fast_profile_for_compatibility(tmp_path):
    c=HLSCapture(tmp_path,synthetic=True,transport='hls',latency_profile='fast')
    assert c.latency_profile=='balanced' and c.frames==960

@pytest.mark.parametrize('profile',['','ultra','bluetooth',None])
def test_invalid_profile(tmp_path,profile):
    with pytest.raises(BridgeError):HLSCapture(tmp_path,transport='pcm',latency_profile=profile)

@pytest.mark.parametrize('failure',[False,True])
def test_windows_scheduling_pairing_simulated(monkeypatch,failure):
    calls=[]
    winmm=SimpleNamespace(timeBeginPeriod=lambda n:calls.append(('begin',n)) or 0,
                          timeEndPeriod=lambda n:calls.append(('end',n)) or 0)
    avrt=SimpleNamespace(AvSetMmThreadCharacteristicsW=lambda task,index:calls.append(('audio',task)) or 7,
                         AvRevertMmThreadCharacteristics=lambda h:calls.append(('revert',h)) or 1)
    monkeypatch.setattr(realtime.sys,'platform','win32')
    monkeypatch.setattr(realtime,'_apis',lambda:(winmm,avrt))
    try:
        with realtime.audio_thread_scope(True) as stats:
            assert stats=={'timer_1ms':True,'mmcss_audio':True}
            if failure:raise RuntimeError('writer failed')
    except RuntimeError:pass
    assert calls==[('begin',1),('audio','Audio'),('revert',7),('end',1)]

def test_scheduling_unavailable_is_not_fatal(monkeypatch):
    monkeypatch.setattr(realtime.sys,'platform','win32')
    def fail():raise OSError('disabled')
    monkeypatch.setattr(realtime,'_apis',fail)
    with realtime.audio_thread_scope(True) as stats:assert not any(stats.values())

async def test_controller_passes_pcm_profile_and_resync_retains_it(tmp_path,monkeypatch):
    c=Controller(tmp_path/'settings');c.glagol=SimpleNamespace(connected=True)
    await c.media.bind('127.0.0.1','127.0.0.1',free_port())
    async def stop():await c.media.stop()
    sent=[]
    async def send(url,title,hls=False):sent.append((url,hls));return {'acknowledged':True}
    monkeypatch.setattr(c,'stop_playback',stop);monkeypatch.setattr(c,'send_audio',send)
    try:
        await c.start_live_playback({'transport':'pcm','latency_profile':'fast'},synthetic=True)
        assert c.live_options['latency_profile']=='fast' and c.media.capture.process is None
        assert sent[0][0].endswith('.wav') and not sent[0][1]
        await c.action('resync',{})
        assert c.live_options['transport']=='pcm' and c.live_options['latency_profile']=='fast'
    finally:
        await c.media.close()
        import shutil;shutil.rmtree(c.cache,ignore_errors=True)


def test_unsupported_pcm_rate_rejected_before_stream_open(tmp_path,monkeypatch):
    p=FakePA(rows=[device(21,'High rate [Loopback]',rate=384000)])
    monkeypatch.setattr(audio,'backend',lambda:fake_backend(p))
    c=HLSCapture(tmp_path,21,transport='pcm')
    with pytest.raises(BridgeError):c.start()
    assert not p.streams and p.terminated
