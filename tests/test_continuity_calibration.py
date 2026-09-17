"""1.0.8 regression tests: policy, actual HTTP, bounded diagnostics and controls.
Windows driver/audio device and physical speaker are deliberately not simulated
as a successful end-to-end acoustic test; those still require the user's PC.
"""
import asyncio
import json
import math
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import aiohttp
from aiohttp import web
import pytest
from station_bridge.continuity import ResumeGate, MaintenancePolicy
from station_bridge.experimental_pcm import SilenceClock, ReceiveTiming
from station_bridge.live_stream import FrameRing
from station_bridge.media import MediaServer
from station_bridge.app import Controller, make_ui_app
from station_bridge.protocol import BridgeError
from test_servers import free_port


def signal(age=0., non_silent=1.):
    return {'last_signal_ago': age, 'non_silent_seconds': non_silent}


def test_resume_only_after_long_silence_and_sustained_signal():
    g=ResumeGate(0)
    assert not g.observe(4.9, signal(None,0))
    assert not g.observe(5.1, signal(None,0)) and g.armed
    assert not g.observe(5.2, signal(0,.05))
    assert g.observe(5.4, signal(0,.14))
    assert not g.observe(5.6, signal(0,.5))
    assert g.triggers==1


@pytest.mark.parametrize('suspended,enabled',[(True,True),(False,False),(True,False)])
def test_resume_disabled_during_pause_or_calibration(suspended,enabled):
    g=ResumeGate(0);g.observe(10,signal(8,0))
    assert not g.observe(10.2,signal(0,1),suspended=suspended,enabled=enabled)
    assert not g.armed


def test_resume_ignores_short_pauses_and_click():
    g=ResumeGate(0)
    for i in range(50):assert not g.observe(i*.1,signal(age=2,non_silent=3))
    assert not g.observe(10,signal(6,3))
    assert not g.observe(10.2,signal(0,3.005))
    assert g.triggers==0


def observe(m,now,**kwargs):
    data=dict(suspended=False,playing=True,has_signal=True,readers=1,
              ever_sent=True,last_data_age=.01,closed_age=2,close_reason='')
    data.update(kwargs)
    return m.observe(now,**data)


def test_ten_minute_timeline_refreshes_at_4_and_8_not_repeatedly():
    m=MaintenancePolicy(0);hits=[]
    for now in range(601):
        reason=observe(m,now)
        if reason:hits.append(now);m.refreshed(now,reason)
    assert hits==[240,480] and m.periodic_count==2


@pytest.mark.parametrize('changes',[{'playing':False},{'suspended':True},{'has_signal':False},{'readers':0},{'last_data_age':5}])
def test_periodic_respects_pause_signal_and_active_reader(changes):
    m=MaintenancePolicy(0)
    assert not observe(m,260,**changes)


def test_disable_periodic_does_not_disable_bounded_repair():
    m=MaintenancePolicy(0,0)
    assert not observe(m,600)
    broken=dict(readers=0,last_data_age=4,close_reason='http_write_timeout')
    for t in (20,34,48):
        assert observe(m,t,**broken)=='http_recovery';m.refreshed(t,'http_recovery')
    assert not observe(m,62,**broken)
    assert observe(m,90,**broken)=='http_recovery'


@pytest.mark.parametrize('step',[.001,.010])
def test_silence_clock_ten_minutes_with_15ms_wakeups_does_not_run_slow(step):
    c=SilenceClock(step,0);total=0;dt=.015625
    for tick in range(1,38401):total+=c.due(tick*dt)
    assert abs(total*step-(600-.020))<=step+1e-6
    assert c.skipped_seconds==0


def test_silence_long_stall_caps_catchup_instead_of_replaying_seconds():
    c=SilenceClock(.001,0);assert c.due(1)<=40 and c.skipped_seconds>.9
    assert c.due(1)==0
    c.reset(2);assert c.due(2.010)==0


@pytest.mark.parametrize('count',[1,10,63,64,65,1024])
def test_atomic_packet_append_keeps_alignment_and_bounded_memory(count):
    r=FrameRing(64,frame_bytes=4,frame_seconds=.001)
    raw=b''.join(i.to_bytes(4,'little') for i in range(count))
    r.append_packet(raw)
    read=r.read(0,2048)
    assert read.data==raw[-64*4:] and read.cursor==count
    assert read.skipped==max(0,count-64)
    assert r.snapshot()['retained_bytes']<=256


@pytest.mark.parametrize('raw',[b'',b'a',b'abcde'])
def test_packet_rejects_incomplete_frames(raw):
    r=FrameRing(8,frame_bytes=4)
    with pytest.raises(ValueError):r.append_packet(raw)


def test_receive_timing_snapshots_during_producer_updates():
    r=ReceiveTiming(48000)
    def write():
        for i in range(4000):r.receive(i*.010,480)
    t=threading.Thread(target=write);t.start()
    for _ in range(100):assert r.snapshot()['callback_count']>=0
    t.join();assert r.snapshot()['callback_count']==4000


async def media(tmp_path,profile='experimental'):
    m=MediaServer(tmp_path,lambda _:None);await m.bind('127.0.0.1','127.0.0.1',free_port())
    url=await m.start_live(None,transport='pcm',latency_profile=profile,synthetic=True)
    return m,url


async def test_soft_resync_revokes_only_url_not_capture_and_retains_history(tmp_path):
    m,url=await media(tmp_path)
    try:
        capture=m.capture;ident=m.capture_id;m.peer_policy.approved='10.0.0.1'
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            r=await s.get(url);await r.content.readexactly(44+192)
            new=await m.refresh_live_url('manual')
            assert new!=url and capture is m.capture and m.capture_id==ident
            assert m.peer_policy.approved=='10.0.0.1' and capture.stats()['running']
            with pytest.raises(aiohttp.ClientPayloadError):await r.read()
            async with s.get(url) as old:assert old.status==404
            r.close()
            m.pcm_response_blocks=50
            async with s.get(new) as fresh:assert (await fresh.read()).startswith(b'RIFF')
            assert m.stats()['soft_restarts']==1
            await m.test_resource()
            assert m.kind=='test' and m.live_history[-1]['media']['capture']['transport']=='pcm'
            assert 'live_edge_refresh' in [e['event'] for e in m.live_history[-1]['media']['stream_events']]
            assert new not in json.dumps(list(m.live_history))
    finally:await m.close()


async def test_400ms_transient_write_does_not_hit_old_250ms_deadline(tmp_path,monkeypatch):
    m,url=await media(tmp_path);m.pcm_response_blocks=200
    original=web.StreamResponse.write;blocked=False
    async def stall(self,data):
        nonlocal blocked
        if len(data)!=44 and not blocked:
            blocked=True;await asyncio.sleep(.40)
        return await original(self,data)
    monkeypatch.setattr(web.StreamResponse,'write',stall)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
            async with s.get(url) as r:
                body=await r.read();assert len(body)==44+200*192
        assert m.live_write_stalls==1 and m.last_close_reason=='finite_response_complete'
    finally:await m.close()


async def test_real_timeout_is_retained_in_diagnostics(tmp_path,monkeypatch):
    m,url=await media(tmp_path);m.live_write_timeout=.03
    original=web.StreamResponse.write
    async def stall(self,data):
        if len(data)!=44:await asyncio.sleep(.1)
        return await original(self,data)
    monkeypatch.setattr(web.StreamResponse,'write',stall)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as s:
            r=await s.get(url)
            with pytest.raises(aiohttp.ClientPayloadError):await r.read()
            r.close()
        assert m.last_close_reason=='http_write_timeout'
        assert m.stats()['stream_events'][-1]['reason']=='http_write_timeout'
    finally:await m.close()


@pytest.mark.parametrize('profile,value,effective',[('fast',5,10),('fast',10,10),('experimental',5,5),('experimental',30,30),('fast',0,0)])
async def test_guard_rounding_and_live_http(profile,value,effective,tmp_path):
    m,url=await media(tmp_path,profile);m.playback_guard_ms=value;m.pcm_response_blocks=12
    try:
        assert m.stats()['micro_buffer_effective_ms']==effective
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get(url) as r:
                assert len(await r.read())==44+12*m.capture.live_ring.frame_bytes
    finally:await m.close()


async def controller(tmp_path):
    c=Controller(tmp_path/'settings')
    c.glagol=SimpleNamespace(connected=True,safe_state=lambda:{'connected':True,'playing':True,'alice':'IDLE','title':'Звук компьютера','state_age_seconds':0})
    async def stop():await c.media.stop()
    async def send(*args,**kwargs):return {'acknowledged':True}
    c.stop_playback=stop;c.send_audio=send
    await c.media.bind('127.0.0.1','127.0.0.1',free_port())
    return c


async def test_calibration_lease_exclusive_and_does_not_start_microphone(tmp_path):
    c=await controller(tmp_path)
    try:
        a=await c.action('calibration_lease',{})
        with pytest.raises(BridgeError):await c.action('calibration_lease',{})
        await c.action('calibration_lease',{'lease':'incorrect','release':True})
        assert c.continuity_snapshot()['calibrating'] and c.media.capture is None
        await c.action('calibration_lease',{'lease':a['lease'],'release':True})
        assert not c.continuity_snapshot()['calibrating']
    finally:await c.media.close();shutil.rmtree(c.cache,ignore_errors=True)


async def test_micro_buffer_guards_stale_capture_and_no_secret_saved(tmp_path):
    c=await controller(tmp_path)
    try:
        await c.start_live_playback({'playback_mode':'main'},synthetic=True)
        with pytest.raises(BridgeError):await c.action('micro_buffer',{'value':10,'capture_id':'old'})
        assert c.media.playback_guard_ms==0
        r=await c.action('micro_buffer',{'value':5,'capture_id':c.media.capture_id})
        assert r['effective_ms']==10 and c.media.soft_restarts==1
        values=dict(median_ms=350,p95_ms=360,spread_ms=15,missed=0,count=6,offset_ms=330,raw_audio='MUST_NOT_SAVE')
        await c.action('calibration_save',{'result':values})
        assert 'raw_audio' not in json.dumps(c.settings.data)
    finally:await c.media.close();shutil.rmtree(c.cache,ignore_errors=True)


@pytest.mark.parametrize('value',[True,float('nan'),float('inf'),-1,31,'5'])
async def test_invalid_buffer_does_not_modify_stream(tmp_path,value):
    c=await controller(tmp_path)
    try:
        with pytest.raises(BridgeError):await c.action('micro_buffer',{'value':value})
        assert c.media.playback_guard_ms==0
    finally:await c.media.close();shutil.rmtree(c.cache,ignore_errors=True)


@pytest.mark.parametrize('value',[True,1,60,300,None])
async def test_invalid_refresh_interval(tmp_path,value):
    c=await controller(tmp_path)
    try:
        with pytest.raises(BridgeError):await c.action('maintenance',{'interval_seconds':value})
        assert c.maintenance.interval==240
    finally:await c.media.close();shutil.rmtree(c.cache,ignore_errors=True)


async def test_controller_periodic_and_no_takeover_after_other_source(tmp_path):
    c=await controller(tmp_path)
    try:
        await c.start_live_playback({'playback_mode':'experimental'},synthetic=True)
        # Only policy input is simulated here; no claim of real Windows capture.
        c.media.capture.synthetic=False
        c.media.capture.input_meter.snapshot=lambda:signal(0,10)
        c.media.live_bytes_sent=1920;c.media.last_request=time.monotonic()
        c.media._live_tasks.add(SimpleNamespace)  # only count inspected; not cancelled by mock below
        c.maintenance.last_refresh-=250
        c.maintenance.last_attempt-=250
        calls=[]
        async def resync(reason):calls.append(reason);c.maintenance.refreshed(time.monotonic(),reason);return {}
        c.soft_resync=resync
        await c.continuity_step();assert calls==['periodic_live_edge']
        c.glagol.safe_state=lambda:{'connected':True,'playing':True,'alice':'IDLE','title':'Другая музыка'}
        c.maintenance.last_refresh-=250
        await c.continuity_step();assert c.recovery_blocked and len(calls)==1
        c.media._live_tasks.clear()
    finally:
        c.media._live_tasks.clear();await c.media.close();shutil.rmtree(c.cache,ignore_errors=True)


async def test_new_pages_are_same_origin_and_api_still_requires_key(tmp_path):
    c=Controller(tmp_path/'settings');await c.start();port=free_port()
    runner=web.AppRunner(make_ui_app(c,'ui-key',port));await runner.setup();await web.TCPSite(runner,'127.0.0.1',port).start()
    base=f'http://127.0.0.1:{port}'
    try:
        async with aiohttp.ClientSession() as s:
            for page in ('calibration.html','calibration.js','calibration-worklet.js','calibration-worker.js','calibration-dsp.js','local-player.html','local-player.js'):
                async with s.get(base+'/'+page) as r:assert r.status==200 and "frame-ancestors 'none'" in r.headers['Content-Security-Policy']
            async with s.post(base+'/api/action',json={'command':'calibration_lease'}) as r:assert r.status==401
            async with s.get(base+'/api/diagnostic',headers={'X-Bridge-Key':'ui-key'}) as r:
                report=await r.json();assert report['version']=='1.0.8' and 'recent_live_sessions' in report
            assert not c.calibration_key
    finally:await runner.cleanup();await c.close()
