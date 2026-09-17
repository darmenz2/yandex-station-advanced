import asyncio
import array
import copy
import math
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import aiohttp
from aiohttp import web
import pytest
from advanced.operations import Operations
from advanced.reliable import ReliableController,setup_config,validate_complete
from advanced.spatial import config,Processor
from advanced.steady_stream import Reader,Timeline,Servo
from advanced.reliable_model import Model
from station_bridge.stream_notify import NotifiedFrameRing
from station_bridge.protocol import BridgeError
from station_bridge.audio import HLSCapture
from station_bridge.media import MediaServer
from advanced.group import BorrowedMedia,GroupController

D=[{'id':x,'name':n,'host':'192.168.1.'+str(i+10),'port':1961,'platform':'test'} for i,(x,n) in enumerate([('a','Лайт слева'),('b','Лайт справа'),('c','Миди')])]

def run(coro):return asyncio.run(coro)

def test_commands_join_not_duplicate():
 async def case():
  op=Operations();calls=0;e=asyncio.Event()
  async def fn():
   nonlocal calls
   calls+=1;await e.wait();return {'message':'checked'}
  a=asyncio.create_task(op.run('live',{},fn));await asyncio.sleep(0.01)
  b=asyncio.create_task(op.run('live',{},fn));await asyncio.sleep(0.01)
  with pytest.raises(BridgeError):await op.run('scan',{},fn)
  assert op.snapshot()['active']['label']=='Запуск передачи';e.set()
  assert await a==await b;assert calls==1
  assert op.snapshot()['last']['detail']=='checked' and not op.snapshot()['busy']
 run(case())

def test_cancel_operation_clears_busy():
 async def case():
  op=Operations();t=asyncio.create_task(op.run('scan',{},lambda:asyncio.sleep(50)));await asyncio.sleep(.01)
  await op.close()
  with pytest.raises(asyncio.CancelledError):await t
  assert op.snapshot()['last']['status']=='cancelled'
 run(case())

@pytest.mark.parametrize('mode',['all','single','stereo','bass21','midbass'])
def test_full_setup(mode):
 ds=D[:1] if mode=='single' else D[:2] if mode=='stereo' else D
 cfg=setup_config({'mode':mode,'roles':{'left':'a','right':'b','bass':'c'}},ds)
 validate_complete(cfg,ds)
 assert cfg['enabled']==(mode not in ('all','single'))
 if mode=='midbass':assert cfg['crossover_hz']==400

@pytest.mark.parametrize('roles',[{'left':'a'},{'left':'a','right':'a','bass':'c'},{'left':'a','right':'b','bass':'d'}])
def test_reject_incomplete_triplet(roles):
 with pytest.raises(BridgeError):setup_config({'mode':'midbass','roles':roles},D)

def test_actual_diagnostic_partial_config_is_rejected():
 with pytest.raises(BridgeError,match='Не назначены каналы'):
  validate_complete({'enabled':True,'layout':'stereo','crossover_hz':120,'speakers':{'a':{'role':'left'}}},D)

def test_stereo_fullscale_no_wrapping():
 raw=array.array('h',[32767,-32768]*480).tobytes()
 p=Processor(48000,2,config(),'a');assert p.process(raw)==raw

@pytest.mark.parametrize('freq',[80,120,400,800,3000])
def test_native_crossover_no_nan_and_bound(freq):
 cfg=setup_config({'mode':'midbass','roles':{'left':'a','right':'b','bass':'c'}},D)
 raw=array.array('h',[int(30000*math.sin(2*math.pi*freq*i/48000)) for i in range(4800) for _ in range(2)]).tobytes()
 for d in D:
  p=Processor(48000,2,cfg,d['id']);out=p.process(raw)
  assert len(out)==len(raw) and p.stats()['clipped_samples']==0

@pytest.mark.parametrize('role,index',[('a',0),('b',1)])
def test_left_right_native_channels(role,index):
 cfg=setup_config({'mode':'stereo','roles':{'left':'a','right':'b'}},D[:2])
 raw=array.array('h',[1000,2500]*480).tobytes()
 result=array.array('h',Processor(48000,2,cfg,role).process(raw))
 assert set(result)=={1000 if index==0 else 2500}

def ring_tone(blocks=50,frames=480,rate=48000):
 r=NotifiedFrameRing(256,frame_bytes=frames*4,frame_seconds=frames/rate)
 for b in range(blocks):r.append(array.array('h',[round(10000*math.sin(2*math.pi*440*(b*frames+i)/rate)) for i in range(frames) for _ in range(2)]).tobytes())
 return r

def test_native_resampler_signal_continuity():
 r=ring_tone();cursor=480*30;reader=Reader(r,480,48000,2,cursor,200)
 all_data=b''.join(reader.take(480) for _ in range(10));out=array.array('h',all_data)[::2]
 ideal=[10000*math.sin(2*math.pi*440*(cursor+i)/48000) for i in range(len(out))]
 err=sum((x-y)**2 for x,y in zip(out,ideal))/len(out)
 assert err<10 and reader.discontinuities==0

def test_servo_cap_slew_and_no_step():
 s=Servo(30);last=1
 for _ in range(5000):
  old,new=s.update(300,.01)
  assert abs(new-last)<=.0000050001;assert .998<=new<=1.0020000001;last=new
 assert abs(last-1.002)<1e-8

def test_servo_virtual_catches_up():
 s=Servo(30);lag=140
 for _ in range(12000):
  a,b=s.update(lag,.01);lag-=(b-1)*10
 assert 15<lag<50

def test_timeline_same_start_with_staggered_connections():
 async def case():
  r=ring_tone();t=Timeline(r,480,['a','b','c'])
  aa=asyncio.create_task(t.join('a'));await asyncio.sleep(.01)
  bb=asyncio.create_task(t.join('b'));await asyncio.sleep(.01)
  cc=asyncio.create_task(t.join('c'))
  vals=await asyncio.gather(aa,bb,cc)
  assert vals[0]==vals[1]==vals[2] and t.snapshot()['arrived']==3;t.cancel()
 run(case())

def test_missing_peer_barrier_does_not_stop_available():
 async def case():
  t=Timeline(ring_tone(),480,['a','b'],timeout=.02)
  a=await asyncio.wait_for(t.join('a'),1);assert a[0]>=0 and t.event.is_set();t.cancel()
 run(case())

def test_gui_immediate_feedback_and_ack():
 sent=[];m=Model(lambda *x:sent.append(x),lambda x:True)
 m.update({'operations':{},'spatial':config(),'desktop':{},'devices':D})
 m.emit('scan');m.emit('scan')
 assert len(sent)==1
 assert any('Поиск' in n.text for n in m.build())
 m.update({'operations':{'last':{'id':1,'status':'done','command':'scan','detail':'Готово'}},'devices':D})
 assert not m.busy()

def test_gui_preset_does_not_commit_before_success():
 sent=[];m=Model(lambda *x:sent.append(x),lambda x:True);m.update({'devices':D,'spatial':config()})
 m.goto('setup');m.selected={'a','b','c'};m.setup_mode='midbass';m.setup_roles={'left':'a','right':'b','bass':'c'}
 m.act('apply_setup');assert m.page=='setup' and len(sent)==1
 m.update({'operations':{'last':{'id':1,'status':'error','command':'apply_setup','detail':'Нет доступа'}},'devices':D})
 assert m.page=='setup';assert any('Нет доступа' in n.text for n in m.build())
 m.emit('apply_setup',m.setup_payload(),True)
 m.update({'operations':{'last':{'id':2,'status':'done','command':'apply_setup','detail':'Готово'}},'devices':D})
 assert m.page=='home'

@pytest.mark.parametrize('page',['home','setup','events','stream','channels','qr','mic','output','advanced'])
def test_gui_pages_no_overlap(page):
 m=Model(lambda *x:None,lambda x:False);m.update({'devices':D,'spatial':config(),'desktop':{}});m.goto(page)
 nodes=m.build(430);content=[n for n in nodes if n.y>=78]
 assert all(n.y+n.h<=n2.y for n,n2 in zip(content,content[1:]))
 assert all(n.x>=0 and n.x+n.w<=430 for n in nodes)

async def make_servers(tmp_path,ids=('a','b','c'),finite=48000,spatial=None):
 owner=MediaServer(tmp_path/'owner',lambda s:None);owner.continuous_pcm=True
 await owner.bind('127.0.0.1','127.0.0.1',0)
 await owner.start_live(None,1,synthetic=True,transport='pcm',latency_profile='fast')
 timeline=Timeline(owner.capture.live_ring,owner.capture.frames,ids)
 servers=[owner]
 for i in range(1,len(ids)):
  child=BorrowedMedia(tmp_path/f'c{i}',lambda s:None);await child.bind('127.0.0.1','127.0.0.1',0);await child.attach(owner);servers.append(child)
 for s,ident in zip(servers,ids):
  s.port=next(iter(s.runner.sites))._server.sockets[0].getsockname()[1]
  s.shared_timeline=timeline;s.timeline_id=ident;s.finite_sample_budget=finite
  s.processor_factory=lambda cap,ident=ident:Processor(cap.rate,cap.channels,spatial,ident)
 return servers,timeline

# Real TCP is used here, without pretending the client is a physical Station.
def test_three_real_http_pcm_readers(tmp_path):
 async def case():
  servers,t=await make_servers(tmp_path)
  try:
   async with aiohttp.ClientSession() as session:
    async def read(s,delay):
     await asyncio.sleep(delay)
     async with session.get(s.current_url()) as r:
      assert r.status==200 and r.headers['Content-Type']=='audio/wav';raw=await r.read()
      assert raw[:4]==b'RIFF' and len(raw)==44+48000*4
      return raw[44:]
    data=await asyncio.gather(*(read(s,i*.05) for i,s in enumerate(servers)))
    assert data[0]==data[1]==data[2]
    assert all(s.last_close_reason=='finite_response_complete' for s in servers)
  finally:
   for s in reversed(servers):await s.close()
   t.cancel()
 run(case())


def fake_peers(controller,ds):
 peers=[]
 for d in ds:
  g=SimpleNamespace(connected=True,send=AsyncMock(return_value={'acknowledged':True}),close=AsyncMock(),
        safe_state=lambda:{'connected':True,'playing':True,'alice':'IDLE','title':'Звук компьютера','state_age_seconds':.1,'volume':.5})
  m=SimpleNamespace(_live_tasks=set(),steady_reader=None,token='stable-token',capture=None,kind='',
                    stop=AsyncMock(),event=lambda *a,**k:None)
  peers.append(SimpleNamespace(device=d,glagol=g,media=m))
 controller.all_peers=lambda:peers
 controller.glagol=peers[0].glagol;controller.selected=ds[0]
 return peers


def test_connect_same_group_preserves_connections(tmp_path):
 async def case():
  c=ReliableController(tmp_path);peers=fake_peers(c,D)
  c.media.kind='live'
  result=await c.connect_group(D)
  assert result['idempotent'] and len(result['results'])==3
  for p in peers:p.glagol.send.assert_not_awaited();p.glagol.close.assert_not_awaited()
 run(case())


def test_start_same_intent_keeps_existing_source(tmp_path):
 async def case():
  c=ReliableController(tmp_path);fake_peers(c,D)
  c.media.kind='live';c.media.capture=SimpleNamespace(stats=lambda:{'running':True})
  c._start_signature=c._launch_key({'playback_mode':'main'},False)
  result=await c.start_live_playback({'playback_mode':'main'})
  assert result['idempotent'];c.glagol.send.assert_not_awaited()
 run(case())


def test_setup_pending_trust_does_not_replace_spatial(tmp_path):
 async def case():
  c=ReliableController(tmp_path);before=copy.deepcopy(c.spatial)
  c.connect_group=AsyncMock(return_value={'trust_required':True})
  c.start_live_playback=AsyncMock();c.stop_playback=AsyncMock()
  result=await c.advanced_action('apply_setup',{'devices':D,'mode':'midbass','roles':{'left':'a','right':'b','bass':'c'}})
  assert result['trust_required'];assert c.spatial==before and c.pending_setup
  c.start_live_playback.assert_not_awaited();c.stop_playback.assert_not_awaited()
  assert c.operations.snapshot()['last']['status']=='needs_confirmation'
 run(case())


def test_setup_partial_failure_is_not_silent(tmp_path):
 async def case():
  c=ReliableController(tmp_path)
  c.connect_group=AsyncMock(return_value={'results':[{'id':'a','connected':True},{'id':'b','connected':False,'error':'TLS'}]})
  c.start_live_playback=AsyncMock()
  with pytest.raises(BridgeError,match='Не подключены все'):
   await c.advanced_action('apply_setup',{'devices':D,'mode':'all'})
  c.start_live_playback.assert_not_awaited()
  assert c.operations.snapshot()['last']['status']=='error'
 run(case())


def test_complete_setup_commits_three_roles_once(tmp_path):
 async def case():
  c=ReliableController(tmp_path)
  c.connect_group=AsyncMock(return_value={'results':[{'id':d['id'],'connected':True} for d in D]})
  c.start_live_playback=AsyncMock(return_value={'acknowledged':True})
  result=await c.advanced_action('apply_setup',{'devices':D,'mode':'midbass','roles':{'left':'a','right':'b','bass':'c'}})
  assert len(c.spatial['speakers'])==3 and c.spatial['crossover_hz']==400
  assert c.pending_setup is None and c.start_live_playback.await_count==1
 run(case())


def test_soft_live_never_sends_stop_or_new_url(tmp_path):
 async def case():
  c=ReliableController(tmp_path);peers=fake_peers(c,D);c.timeline=object();c.media.kind='live'
  for p in peers:p.media.steady_reader=SimpleNamespace(request_live=lambda:None)
  result=await c.soft_resync()
  assert 'не сбрасывались' in result['message']
  assert all(p.media.token=='stable-token' for p in peers)
  for p in peers:p.glagol.send.assert_not_awaited()
 run(case())


def test_peer_retry_never_interrupts_other_peers(tmp_path):
 async def case():
  c=ReliableController(tmp_path);peers=fake_peers(c,D);c.media.kind='live';c.media.capture=object()
  for p in peers:
   p.media.peer_policy=SimpleNamespace(pending=lambda token:None)
   p.media.current_url=lambda:'http://127.0.0.1/audio/live.wav';p.media.title='Звук компьютера';p.media.transport='pcm'
  result=await c.retry_peer('b',explicit=True)
  assert peers[1].glagol.send.await_count==1
  peers[0].glagol.send.assert_not_awaited();peers[2].glagol.send.assert_not_awaited()
  peers[1].media._live_tasks.add(object())
  result=await c.retry_peer('b',explicit=True);assert result['idempotent']
  assert peers[1].glagol.send.await_count==1
 run(case())


def test_new_http_security_and_head(tmp_path):
 async def case():
  servers,t=await make_servers(tmp_path,('a',),finite=4800)
  try:
   s=servers[0]
   async with aiohttp.ClientSession() as session:
    async with session.head(s.current_url()) as r:
     assert r.status==200 and len(await r.read())==0 and s.live_connections==0
    async with session.get(s.current_url().replace(s.token,'invalid-token')) as r:assert r.status==404
    s.peer_policy.allows=lambda peer:False
    async with session.get(s.current_url()) as r:assert r.status==403
    assert s.live_connections==0
  finally:
   for s in reversed(servers):await s.close()
   t.cancel()
 run(case())


def test_processor_probe_uses_sample_timeline():
 from advanced.calibration import Plan,ProbeProcessor
 plan=Plan(['a']);plan.arm(0)
 cap=SimpleNamespace(frames=480,channels=2)
 p=ProbeProcessor(cap,plan,'a')
 raw=bytes(480*4);start=plan.trials[0]['sample']
 assert any(p.process_samples(raw,start)) and not any(p.process_samples(raw,0))


def test_gap_recovery_keeps_filter_state_and_counts_it():
 r=ring_tone();p=Reader(r,480,48000,2,480*48,30)
 first=p.take(400)
 for _ in range(300):r.append(bytes(480*4))
 result=p.take(480)
 assert result and p.discontinuities==1 and p.skipped_samples>0
 assert len(result)%4==0


def test_apply_same_preset_preserves_measured_delays(tmp_path):
 async def case():
  c=ReliableController(tmp_path);fake_peers(c,D)
  intent={'mode':'midbass','roles':{'left':'a','right':'b','bass':'c'}}
  c.spatial=setup_config(intent,D);c.spatial['speakers']['a']['delay_ms']=45
  c._last_setup=copy.deepcopy(intent)
  c.connect_group=AsyncMock(return_value={'results':[{'id':d['id'],'connected':True} for d in D]})
  c.start_live_playback=AsyncMock(return_value={'idempotent':True})
  await c.apply_setup({'devices':D,**intent})
  assert c.spatial['speakers']['a']['delay_ms']==45
 run(case())


def test_one_disconnected_http_reader_does_not_stop_other_two(tmp_path):
 async def case():
  servers,t=await make_servers(tmp_path,finite=48000)
  try:
   async with aiohttp.ClientSession() as session:
    async def read(s,early=False):
     async with session.get(s.current_url()) as r:
      if early:
       await r.content.readexactly(44+9600);r.close();return None
      return await r.read()
    results=await asyncio.gather(read(servers[0]),read(servers[1],True),read(servers[2]))
    assert len(results[0])==len(results[2])==44+192000
    assert servers[0].capture.stats()['running']
    assert servers[0].token and servers[2].token
  finally:
   for s in reversed(servers):await s.close()
   t.cancel()
 run(case())


def test_receive_driven_main_has_no_fake_silence_on_normal_callbacks(tmp_path):
 import threading
 from station_bridge.experimental_pcm import run_receive_driven,ReceiveTiming
 c=HLSCapture(tmp_path,transport='pcm',latency_profile='fast');c.continuous_pcm=True
 c.experimental_timing=ReceiveTiming(c.rate)
 c.pcm_ring=NotifiedFrameRing(256,frame_bytes=c.frames*4,frame_seconds=.01)
 th=threading.Thread(target=run_receive_driven,args=(c,),daemon=True);th.start()
 try:
  for _ in range(24):
   now=time.monotonic();c.queue.put((now,bytes(1920)));time.sleep(.01)
  time.sleep(.015)
  assert c.written_frames==24*480 and c.silence_frames==0 and not c.error
 finally:c.stop_event.set();th.join(2)


def test_long_idle_stays_silent_and_resume_no_url_reset(tmp_path):
 import threading
 from station_bridge.experimental_pcm import run_receive_driven,ReceiveTiming
 c=HLSCapture(tmp_path,transport='pcm',latency_profile='fast');c.continuous_pcm=True
 c.experimental_timing=ReceiveTiming(c.rate)
 c.pcm_ring=NotifiedFrameRing(256,frame_bytes=c.frames*4,frame_seconds=.01)
 th=threading.Thread(target=run_receive_driven,args=(c,),daemon=True);th.start()
 try:
  time.sleep(.18)
  assert c.silence_frames>0
  raw=c.pcm_ring.read(0,256).data;assert not any(raw)
  c.queue.put((time.monotonic(),array.array('h',[300]*960).tobytes()));time.sleep(.012)
  assert 300 in array.array('h',c.pcm_ring.read(0,256).data)
 finally:c.stop_event.set();th.join(2)


def test_first_samples_not_discarded_for_interpolator_history():
 ring=ring_tone();reader=Reader(ring,480,48000,2,0,500,False)
 raw=reader.take(480)
 assert len(raw)==1920 and reader.cursor==480
 assert reader.skipped_samples==0 and reader.discontinuities==0


def test_common_start_uses_explicit_history_padding():
 async def case():
  ring=ring_tone(blocks=2);t=Timeline(ring,480,['one'],target_ms=30)
  cursor,wall=await t.join('one');assert cursor==0
  r=Reader(ring,480,48000,2,cursor,30);assert r.take(480)
  assert not r.discontinuities;t.cancel()
 run(case())
