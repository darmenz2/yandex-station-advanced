"""1.0.7: simulated capture, real PCM/HTTP, bounded event handoff.

These tests do not measure a native Windows driver or a physical speaker.
"""
from __future__ import annotations
import array
import asyncio
import contextlib
import io
import json
from pathlib import Path
import queue
import struct
import threading
import time
from types import SimpleNamespace
import wave
from unittest.mock import AsyncMock
import aiohttp
from aiohttp import web
import pytest
from station_bridge import audio
from station_bridge.audio import HLSCapture
from station_bridge.app import Controller
from station_bridge.experimental_pcm import ReceiveTiming
from station_bridge.media import MediaServer
from station_bridge.protocol import BridgeError
from station_bridge.stream_notify import NotifiedFrameRing
from test_live_capture_fix import FakePA, device, fake_backend, tone
from test_servers import free_port


def wait_until(check, timeout=2):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if check(): return
        time.sleep(.002)
    assert check(), 'condition did not become true'


@pytest.mark.parametrize('transport,profile,block,jitter,age', [
    ('pcm','fast',10,.020,.060), ('pcm','balanced',20,.060,.200),
    ('mp3','fast',10,.020,.060), ('hls','fast',20,.060,.200),
    ('pcm','experimental',1,0.,.050)])
def test_profiles_remain_separate(tmp_path,transport,profile,block,jitter,age):
    c=HLSCapture(tmp_path,transport=transport,latency_profile=profile)
    assert c.block_ms == block and c.start_buffer_seconds == jitter and c.max_pcm_age == age
    assert c.experimental == (profile == 'experimental')

@pytest.mark.parametrize('mode',['hls','mp3'])
def test_experiment_is_pcm_only(tmp_path,mode):
    with pytest.raises(BridgeError):HLSCapture(tmp_path,transport=mode,latency_profile='experimental')

@pytest.mark.parametrize('rate,channels',[(8000,1),(44100,2),(48000,2),(96000,2),(192000,1)])
def test_native_rate_and_one_ms_request_no_resampler(tmp_path,monkeypatch,rate,channels):
    p=FakePA(rows=[device(21,'Output [Loopback]',rate=rate,channels=channels)],modes={21:'tone'})
    monkeypatch.setattr(audio,'backend',lambda:fake_backend(p))
    def no_encoder():raise AssertionError('No FFmpeg in either PCM path')
    monkeypatch.setattr(audio,'ffmpeg_executable',no_encoder)
    c=HLSCapture(tmp_path,21,transport='pcm',latency_profile='experimental');c.start()
    try:
        wait_until(c.ready)
        wait_until(lambda: c.experimental_timing.count>=6)
        assert p.streams[0].kwargs['frames_per_buffer']==max(1,round(rate/1000))
        assert (c.rate,c.channels)==(rate,channels) and c.process is None
        assert c.stats()['running'] and c.stats()['startup_jitter_ms']==0
        assert c.stats()['experimental_timing']['callback_packet_ms']['p50'] is not None
        assert c.pcm_ring.frame_bytes==c.frames*channels*2
    finally:c.stop()
    assert p.terminated and not c.writer.is_alive()


def capture_without_producer(tmp_path,monkeypatch):
    p=FakePA(rows=[device(21,'Manual [Loopback]')],modes={21:'none'})
    monkeypatch.setattr(audio,'backend',lambda:fake_backend(p))
    c=HLSCapture(tmp_path,21,transport='pcm',latency_profile='experimental');c.start()
    return c,p.streams[0].kwargs['stream_callback']


def test_larger_real_callback_preserves_all_samples(tmp_path,monkeypatch):
    c,callback=capture_without_producer(tmp_path,monkeypatch)
    try:
        # Real drivers may ignore 1 ms and supply 10 ms at once. All ten
        # complete PCM units must be published, without an extra pacing timer.
        raw=b'\x23\x01'*960
        received=time.monotonic();callback(raw,480,{},0)
        wait_until(lambda:c.written_frames>=480)
        frames=c.pcm_ring.read(0,10).data
        assert frames==raw
        assert c.experimental_timing.last_frames==480
        assert c.experimental_timing.last_dispatch_ms < 100
        assert c.input_meter.snapshot()['received_frames']==480
    finally:c.stop()


def test_partial_unit_alignment_preserved(tmp_path,monkeypatch):
    c,callback=capture_without_producer(tmp_path,monkeypatch)
    try:
        half=c.pcm_ring.frame_bytes//2
        raw=b'\x16\x02'*(half//2)
        callback(raw,24,{},0);callback(raw,24,{},0)
        wait_until(lambda:c.written_frames>=48)
        assert c.pcm_ring.read(0,1).data==raw+raw
    finally:c.stop()


def test_unaligned_callback_aborts_and_reports(tmp_path,monkeypatch):
    c,callback=capture_without_producer(tmp_path,monkeypatch)
    try:
        _,flag=callback(b'\x00',1,{},0)
        assert flag==2 and c.error
    finally:c.stop()


def test_stale_callback_dropped_not_replayed(tmp_path,monkeypatch):
    c,callback=capture_without_producer(tmp_path,monkeypatch)
    try:
        c.queue.put((time.monotonic()-1,b'\x23\x01'*96))
        wait_until(lambda:c.stale_frames>=48)
        assert c.dropped>=1
        assert b'\x23\x01' not in c.pcm_ring.read(0,64).data
    finally:c.stop()


def test_idle_keepalive_does_not_busy_loop(tmp_path,monkeypatch):
    c,callback=capture_without_producer(tmp_path,monkeypatch)
    try:
        time.sleep(.10)
        assert c.silence_frames>0
        assert c.experimental_timing.idle_packets<20
        assert c.written_frames < 48000*.15
        assert c.stats()['encoder_active'] is False
    finally:c.stop()


def test_input_resumes_after_silence(tmp_path,monkeypatch):
    c,callback=capture_without_producer(tmp_path,monkeypatch)
    try:
        time.sleep(.06)
        callback(tone(480),480,{},0)
        wait_until(lambda:c.experimental_timing.dispatches>0)
        assert c.input_meter.snapshot()['peak_since_start']>0
        assert c.experimental_timing.last_dispatch_ms<100
    finally:c.stop()


@pytest.mark.parametrize('profile',['fast','experimental'])
def test_producer_stop_no_leaked_thread(tmp_path,profile):
    c=HLSCapture(tmp_path,synthetic=True,transport='pcm',latency_profile=profile);c.start()
    wait_until(c.ready);c.stop();c.stop()
    assert not c.writer.is_alive()


async def test_notify_wakes_without_polling():
    ring=NotifiedFrameRing(8,frame_bytes=4,frame_seconds=.001)
    sub=ring.subscribe();sub.clear()
    thread=threading.Thread(target=lambda:ring.append(b'abcd'));thread.start();thread.join()
    await sub.wait(.5)
    assert ring.read(0).data==b'abcd'
    sub.close();assert ring.subscriber_count==0


async def test_notify_coalesces_and_supports_two_readers():
    ring=NotifiedFrameRing(16,frame_bytes=4,frame_seconds=.001)
    a=ring.subscribe();b=ring.subscribe()
    def push():
        for _ in range(16):ring.append(b'abcd')
    t=threading.Thread(target=push);t.start();t.join()
    await asyncio.gather(a.wait(.5),b.wait(.5))
    assert len(ring.read(0,16).data)==64
    a.close();b.clear()
    ring.append(b'efgh');await b.wait(.5)
    b.close();assert ring.subscriber_count==0


async def test_clear_read_wait_does_not_lose_already_written_data():
    ring=NotifiedFrameRing(8,frame_bytes=4,frame_seconds=.001)
    sub=ring.subscribe();ring.append(b'abcd')
    await asyncio.sleep(0)
    sub.clear()
    assert ring.read(0).data==b'abcd'  # never wait when data already exists
    sub.close()


async def test_closed_subscriber_and_ring_remain_usable():
    ring=NotifiedFrameRing(8,frame_bytes=4,frame_seconds=.001)
    sub=ring.subscribe();sub.close();ring.append(b'abcd');await asyncio.sleep(0)
    assert not sub.event.is_set()
    assert ring.snapshot()['encoded_frames']==1


async def make_server(tmp_path,profile='experimental'):
    m=MediaServer(tmp_path,lambda _:None);await m.bind('127.0.0.1','127.0.0.1',free_port())
    url=await m.start_live(None,transport='pcm',latency_profile=profile,synthetic=True)
    return m,url


async def test_experimental_real_http_wav_length_head_range(tmp_path):
    m,url=await make_server(tmp_path);m.pcm_response_blocks=100
    try:
        async with aiohttp.ClientSession() as s:
            async with s.head(url) as r:
                assert r.status==200 and int(r.headers['Content-Length'])==44+100*192
                assert not await r.read() and m.capture.pcm_ring.subscriber_count==0
            async with s.get(url,headers={'Range':'bytes=0-'}) as r:
                assert r.status==200 and 'Transfer-Encoding' not in r.headers
                body=await r.read();assert len(body)==44+100*192
                with wave.open(io.BytesIO(body),'rb') as w:
                    assert (w.getframerate(),w.getnchannels(),w.getnframes())==(48000,2,4800)
                    assert max(map(abs,array.array('h',w.readframes(4800))))>2000
        assert m.live_bytes_sent==19200
        await asyncio.sleep(.01)
        assert m.capture.pcm_ring.subscriber_count==0
    finally:await m.close()


async def test_stop_revokes_stream_and_wakes_cleanup(tmp_path):
    m,url=await make_server(tmp_path)
    try:
        async with aiohttp.ClientSession() as s:
            r=await s.get(url);await r.content.readexactly(44+192)
            ring=m.capture.pcm_ring;c=m.capture
            assert ring.subscriber_count==1
            await asyncio.wait_for(m.stop(),3)
            with pytest.raises(aiohttp.ClientPayloadError):await r.read()
            assert ring.subscriber_count==0 and not c.writer.is_alive() and not m._live_tasks
            async with s.get(url) as old:assert old.status==404
            r.close()
    finally:await m.close()


async def test_experimental_readers_capped_and_bad_urls_rejected(tmp_path):
    m,url=await make_server(tmp_path)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url.replace(m.token,'invalid')) as r:assert r.status==404
            a=await s.get(url);b=await s.get(url)
            await a.content.readexactly(44);await b.content.readexactly(44)
            async with s.get(url) as r:assert r.status==503
            a.close();b.close()
    finally:await m.close()


async def test_nat_approval_not_bypassed_in_experiment(tmp_path):
    m,url=await make_server(tmp_path)
    try:
        request=SimpleNamespace(remote='10.0.0.1',method='GET',match_info={'token':m.token,'name':'live.wav'})
        with pytest.raises(web.HTTPForbidden):await m.handle(request)
        assert m.peer_policy.pending(m.token)['ip']=='10.0.0.1'
        assert m.live_connections==0
    finally:await m.close()


async def prepare_controller(tmp_path,monkeypatch):
    c=Controller(tmp_path/'settings');c.glagol=SimpleNamespace(connected=True)
    await c.media.bind('127.0.0.1','127.0.0.1',free_port())
    async def stop():await c.media.stop()
    async def send(*args,**kwargs):return {'acknowledged':True}
    monkeypatch.setattr(c,'stop_playback',stop);monkeypatch.setattr(c,'send_audio',send)
    return c


@pytest.mark.parametrize('params,transport,profile',[
    ({},'pcm','fast'),({'playback_mode':'main','transport':'mp3','latency_profile':'balanced'},'pcm','fast'),
    ({'playback_mode':'experimental'},'pcm','experimental'),
    ({'playback_mode':'fallback','transport':'mp3','latency_profile':'balanced'},'mp3','balanced'),
    ({'playback_mode':'fallback','transport':'pcm','latency_profile':'balanced'},'pcm','balanced')])
async def test_controller_main_default_and_explicit_fallback(tmp_path,monkeypatch,params,transport,profile):
    c=await prepare_controller(tmp_path,monkeypatch)
    try:
        await c.start_live_playback(params,synthetic=True)
        assert c.media.capture.transport==transport and c.media.capture.latency_profile==profile
        await c.action('resync',{})
        assert c.media.capture.transport==transport and c.media.capture.latency_profile==profile
    finally:
        await c.media.close()
        import shutil;shutil.rmtree(c.cache,ignore_errors=True)


async def test_bad_experiment_request_does_not_stop_working_primary(tmp_path,monkeypatch):
    c=await prepare_controller(tmp_path,monkeypatch)
    try:
        await c.start_live_playback({},synthetic=True)
        original=c.media.capture
        with pytest.raises(BridgeError):
            await c.start_live_playback({'playback_mode':'fallback','transport':'mp3','latency_profile':'experimental'},synthetic=True)
        assert c.media.capture is original and original.stats()['running']
    finally:
        await c.media.close()
        import shutil;shutil.rmtree(c.cache,ignore_errors=True)


def test_timing_history_is_bounded_and_reports_actual_packets():
    timing=ReceiveTiming(48000)
    for i in range(2000):timing.receive(i*.01,480)
    stats=timing.snapshot()
    assert len(timing.intervals)==512 and len(timing.durations)==512
    assert stats['callback_packet_ms']['p50']==10
    assert stats['callback_interval_ms']['p95']==10
    assert stats['callback_count']==2000

async def test_event_loop_stall_skips_backlog_without_growing_ring(tmp_path):
    m,url=await make_server(tmp_path)
    try:
        async with aiohttp.ClientSession() as s:
            r=await s.get(url);await r.content.readexactly(44+192)
            time.sleep(.18)  # intentionally stall HTTP loop; producer is a thread
            await r.content.readexactly(192*8)
            await asyncio.sleep(.06)
            assert m.capture.pcm_ring.snapshot()['retained_bytes']<=64*192
            assert m.live_skipped_frames>0
            assert m.capture.transport=='pcm' and m.capture.latency_profile=='experimental'
            assert m.capture.stats()['running']
            r.close()
    finally:await m.close()


def test_callback_status_flags_retained_in_experiment():
    timing=ReceiveTiming(48000);timing.receive(1.,48,2);timing.receive(1.001,48,0)
    assert timing.snapshot()['callback_status_flags']==1
