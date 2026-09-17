"""Source-clock regression tests. No native Windows/physical speaker is claimed."""
from __future__ import annotations
import array
import asyncio
import base64
import copy
import math
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import aiohttp
import pytest
from advanced.steady_stream import FifoReader, Timeline
from advanced.reliable import ReliableController
from advanced.reliable_model import Model
from station_bridge.stream_notify import NotifiedFrameRing
from station_bridge.protocol import proto_string, varint, BridgeError
from station_bridge.native_state import observe_app_state, configured_pairs, stereo_configuration, wire
from station_bridge import audio
from tests.test_reliable import D, fake_peers, make_servers
from tests.test_live_capture_fix import FakePA, device, fake_backend
from tests.test_experimental_pcm import wait_until


def block(b, frames=480, channels=2):
    return array.array('h',[((b*frames+i)*17+c*1927)%50000-25000 for i in range(frames) for c in range(channels)]).tobytes()


@pytest.mark.parametrize('frames',[48,480,960])
@pytest.mark.parametrize('channels',[1,2,6,8])
def test_fifo_exact_samples_not_filter_or_clock(frames,channels):
    ring=NotifiedFrameRing(256,frame_bytes=frames*channels*2,frame_seconds=frames/48000)
    incoming=[block(i,frames,channels) for i in range(190)]
    ring.append_packet(b''.join(incoming))
    r=FifoReader(ring,frames,48000,channels,0,80)
    pieces=[]
    for wanted in [17,frames,31,3*frames,43,9*frames]*40:
        data=r.take(wanted)
        if not data:break
        pieces.append(data)
    received=b''.join(pieces)
    assert received==b''.join(incoming)[:len(received)]
    assert len(received)==190*frames*channels*2
    assert r.stats()['rate_correction_ppm']==0
    assert r.stats()['discontinuities']==0


def test_fifo_survives_batched_producer_and_slow_consumers():
    ring=NotifiedFrameRing(512,frame_bytes=1920,frame_seconds=.01)
    readers=[FifoReader(ring,480,48000,2,0,120) for _ in range(3)]
    output=[bytearray(),bytearray(),bytearray()];source=bytearray()
    # Deliberately uneven delivery: 10-150ms batches, not a perfect 10ms source.
    batch_sizes=[1,1,12,1,4,1,15,2,3,1]*4
    pos=0
    for step,units in enumerate(batch_sizes):
        payload=b''.join(block(i) for i in range(pos,pos+units));pos+=units
        source.extend(payload);ring.append_packet(payload)
        for i,r in enumerate(readers):
            if step%(i+2):
                while data:=r.take(1920):output[i].extend(data)
    for i,r in enumerate(readers):
        while data:=r.take(1920):output[i].extend(data)
        assert output[i]==source[:len(output[i])]
        assert r.skipped_samples==0
    assert output[0]==output[1]==output[2]


def test_fifo_loss_is_counted_only_after_actual_overwrite():
    ring=NotifiedFrameRing(64,frame_bytes=1920,frame_seconds=.01)
    r=FifoReader(ring,480,48000,2,0,120)
    for i in range(100):ring.append(block(i))
    assert r.take(480) and r.discontinuities==1 and r.skipped_samples==36*480
    for _ in range(10):r.take(480)
    assert r.discontinuities==1


def test_fast_retention_is_not_default_playback_delay(tmp_path,monkeypatch):
    p=FakePA(rows=[device(21,'Manual [Loopback]')],modes={21:'none'})
    monkeypatch.setattr(audio,'backend',lambda:fake_backend(p))
    c=audio.HLSCapture(tmp_path,21,transport='pcm',latency_profile='fast');c.continuous_pcm=True;c.start()
    try:
        assert c.block_ms==10 and c.max_pcm_age==2 and c.queue.maxsize>=100
        # This delayed valid block was dropped as "stale" by preview.4 (60ms).
        expected=block(0)
        c.queue.put((time.monotonic()-.150,expected))
        wait_until(lambda:c.written_frames>=480)
        assert c.pcm_ring.read(0,1).data==expected
        assert c.dropped==c.silence_frames==c.stale_frames==0
    finally:c.stop()


def test_normal_jitter_gap_does_not_invent_silence(tmp_path,monkeypatch):
    p=FakePA(rows=[device(21,'Manual [Loopback]')],modes={21:'none'})
    monkeypatch.setattr(audio,'backend',lambda:fake_backend(p))
    c=audio.HLSCapture(tmp_path,21,transport='pcm',latency_profile='fast');c.continuous_pcm=True;c.start()
    try:
        call=p.streams[0].kwargs['stream_callback']
        call(block(0),480,{},0);time.sleep(.10)
        call(b''.join(block(i) for i in range(1,11)),4800,{},0)
        wait_until(lambda:c.written_frames>=5280)
        assert c.silence_frames==0
        assert c.pcm_ring.read(0,11).data==b''.join(block(i) for i in range(11))
    finally:c.stop()


@pytest.mark.asyncio
async def test_actual_http_three_receivers_under_loop_stalls(tmp_path):
    servers,timeline=await make_servers(tmp_path,finite=48000*2+17)
    timeline.target_ms=120
    try:
        for s in servers:s.smooth_enabled=False
        async def stall_loop():
            for _ in range(10):
                await asyncio.sleep(.10)
                time.sleep(.065)  # emulated GUI/event-loop block, intentionally not async
        async with aiohttp.ClientSession() as session:
            async def take(s,index):
                async with session.get(s.current_url()) as resp:
                    assert resp.status==200
                    data=bytearray()
                    async for chunk in resp.content.iter_chunked(1703):
                        data.extend(chunk)
                        if index and len(data)%7==0:await asyncio.sleep(.005)
                    return bytes(data)
            tasks=[asyncio.create_task(take(s,i)) for i,s in enumerate(servers)]
            staller=asyncio.create_task(stall_loop())
            result=await asyncio.wait_for(asyncio.gather(*tasks),10)
            await staller
        assert len(result[0])==44+(48000*2+17)*4
        assert result[0]==result[1]==result[2]
        for s in servers:
            assert s.last_close_reason=='finite_response_complete'
            assert s.last_steady_stats['discontinuities']==0
            assert s.last_steady_stats['method']=='source_sample_fifo'
    finally:
        for s in reversed(servers):await s.close()
        timeline.cancel()


def proto_bytes(field,value):return varint(field*8+2)+varint(len(value))+value

def proto_int(field,value):return varint(field*8)+varint(value)


def test_native_observation_whitelist_no_secrets():
    audio_state=(proto_string(2,'http://private/resource-token')+proto_int(13,1100000000)+proto_int(14,97000000)
                 +proto_string(23,'audio-clock-123')+proto_string(26,'MULTIROOM_SECRET')
                 +proto_int(36,2)+proto_bytes(30,proto_string(1,'slave')+proto_string(2,'SESSION_SECRET')))
    event=proto_int(2,3)+proto_bytes(3,audio_state)+proto_int(17,1)
    raw=proto_bytes(6,event)
    result=observe_app_state({'extra':{'appState':base64.b64encode(raw).decode(),'token':'SECRET'}})
    assert result['basetime_ns']==1100000000 and result['multiroom_mode']=='slave'
    assert result['has_audio_focus'] and len(result['clock_fingerprint'])==16
    assert 'SECRET' not in repr(result) and 'http' not in repr(result) and 'audio-clock-123' not in repr(result)
    assert result['clock_control_available'] is False


@pytest.mark.parametrize('raw',[b'\x00',b'\x80'*12,b'\x12\x10bad',b'\xff'*140000,b'\x0b'])
def test_native_decoder_rejects_invalid_bytes(raw):
    assert observe_app_state({'extra':{'appState':base64.b64encode(raw).decode()}})=={}


@pytest.mark.parametrize('message',[{}, {'extra':{}}, {'extra':[]}, {'extra':{'appState':'@invalid@'}}, {'extra':{'appState':0}}])
def test_native_decoder_missing_does_not_invent_state(message):
    assert observe_app_state(message)=={}


def pair_inventory():
    return [{**D[0],'native_stereo':{'role':'leader','channel':'left','partner_id':'b'}},
            {**D[1],'native_stereo':{'role':'follower','channel':'right','partner_id':'a'}},D[2]]


def test_only_reciprocal_native_pairs_are_accepted():
    ds=pair_inventory();p=configured_pairs(ds)
    assert len(p)==1 and p[0]['leader_id']=='a' and p[0]['pcm_supported'] is None
    ds[1]['native_stereo']['partner_id']='elsewhere';assert not configured_pairs(ds)
    assert not configured_pairs([{'id':'fake','name':'Яндекс Лайт 2'}])


def test_native_stereo_config_does_not_copy_device_secrets():
    data={'config':{'stereo_pair':{'role':'leader','channel':'left','partnerDeviceId':'b','token':'secret'},'access_token':'secret'}}
    assert stereo_configuration(data)=={'role':'leader','channel':'left','partner_id':'b'}


@pytest.mark.asyncio
async def test_native_music_requires_explicit_global_consent(tmp_path):
    c=ReliableController(tmp_path);peers=fake_peers(c,D)
    c.stop_playback=AsyncMock()
    with pytest.raises(BridgeError):await c.advanced_action('native_music',{})
    c.stop_playback.assert_not_awaited()
    for p in peers:p.glagol.send.assert_not_awaited()
    await c.advanced_action('native_music',{'confirmed':True})
    assert peers[0].glagol.send.call_args.args[0]=={'command':'sendText','text':'включи музыку везде'}
    for p in peers[1:]:p.glagol.send.assert_not_awaited()
    again=await c.advanced_action('native_music',{'confirmed':True});assert again['idempotent']
    assert peers[0].glagol.send.await_count==1
    with pytest.raises(BridgeError):await c.start_live_playback({'playback_mode':'main'})


@pytest.mark.asyncio
async def test_pair_probe_routes_pcm_only_to_verified_leader(tmp_path):
    c=ReliableController(tmp_path);peers=fake_peers(c,D)
    c.auth=SimpleNamespace(logged_in=True,devices=AsyncMock(return_value=pair_inventory()))
    c.stop_playback=AsyncMock();c.connect_group=AsyncMock(return_value={'results':[{'id':'a','connected':True}]})
    c.start_live_playback=AsyncMock(return_value={})
    await c.advanced_action('native_pair_probe',{'leader_id':'a','confirmed':True})
    assert c.connect_group.call_args.args==([D[0]],)
    assert c.spatial['enabled'] is False
    assert c.start_live_playback.call_args.args==({'playback_mode':'main'},)
    assert c.native_source['status']=='forwarding_unverified'
    for p in peers:p.glagol.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverified_pair_does_not_replace_audio(tmp_path):
    c=ReliableController(tmp_path);fake_peers(c,D)
    c.auth=SimpleNamespace(logged_in=True,devices=AsyncMock(return_value=D))
    c.stop_playback=AsyncMock();c.connect_group=AsyncMock()
    with pytest.raises(BridgeError):await c.advanced_action('native_pair_probe',{'leader_id':'a','confirmed':True})
    c.stop_playback.assert_not_awaited();c.connect_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_pair_cannot_be_sent_two_independent_channels(tmp_path):
    c=ReliableController(tmp_path);c.native_loaded_at=time.monotonic();c.native_inventory=pair_inventory()
    c.connect_group=AsyncMock()
    with pytest.raises(BridgeError,match='штатная стереопара'):
        await c.apply_setup({'devices':D[:2],'mode':'stereo','roles':{'left':'a','right':'b'}})
    c.connect_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_stop_failure_is_visible_without_locking_cleanup(tmp_path):
    c=ReliableController(tmp_path);peers=fake_peers(c,D);peers[0].glagol.connected=False
    c.native_source={'mode':'yandex_music','control_id':'a'}
    await c.stop_native_music()
    assert c.native_source['status']=='stop_unconfirmed' and c.native_source.get('warning')


@pytest.mark.parametrize('page',['home','native','stream','advanced'])
def test_new_native_gui_page_layout_and_no_implicit_commands(page):
    sent=[];m=Model(lambda *x:sent.append(x),lambda x:False)
    m.update({'desktop':{},'yandex_native':{'pairs':configured_pairs(pair_inventory()),'peers':[]}})
    m.goto(page);nodes=m.build(430)
    assert not sent
    content=[n for n in nodes if n.y>=78]
    assert all(a.y+a.h<=b.y for a,b in zip(content,content[1:]))
    assert all(0<=n.x and n.x+n.w<=430 for n in nodes)
    m.act('native_music');assert not sent


def test_buffer_migration_turns_off_legacy_resampling(tmp_path):
    c=ReliableController(tmp_path)
    assert c.smooth_enabled is False and c.pcm_reserve_ms==120
    assert c.settings.data['pcm_policy_revision']==5


@pytest.mark.asyncio
async def test_no_midstream_buffer_jump(tmp_path):
    c=ReliableController(tmp_path);c.media.kind='live'
    with pytest.raises(BridgeError):await c.advanced_action('pcm_reserve',{'ms':300})
    assert c.pcm_reserve_ms==120
    c.media.kind='';await c.advanced_action('pcm_reserve',{'ms':200})
    assert c.pcm_reserve_ms==200
