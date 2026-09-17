"""Explicit, bounded microphone calibration. No audio files or network uploads.

Estimates RELATIVE transport/playback timing with sequential broad-band probes.
It does not measure room EQ, acoustic crossover phase or speaker-clock drift.
Crossover and per-peer delay are bypassed for the probe. All HTTP readers remain
open and receive silence except the peer being measured.
"""
from __future__ import annotations
import array
import asyncio
import contextlib
import ctypes as C
import hashlib
import json
import math
import statistics
import threading
import time
from .spatial import library
from station_bridge.protocol import BridgeError


def probe(rate):
    length=round(rate*.080)
    vals=[math.sin(2*math.pi*(450*(i/rate)+(.5*(1850-450)/.080)*(i/rate)**2)) *
          math.sin(math.pi*i/(length-1))**2*.08 for i in range(length)]
    mean=sum(vals)/len(vals)
    return [v-mean for v in vals]


def microphones():
    from station_bridge.loopback import backend
    pa=backend();out=[]
    with pa.PyAudio() as p:
        for i in range(p.get_device_count()):
            d=p.get_device_info_by_index(i)
            if d.get('isLoopbackDevice') or d.get('maxInputChannels',0)<1:continue
            host=p.get_host_api_info_by_index(d['hostApi'])['name']
            if 'WASAPI' not in host:continue
            out.append({'index':i,'name':d['name'],'rate':int(d['defaultSampleRate']),
                        'channels':min(2,int(d['maxInputChannels'])),'host_api':host})
    return out


class Recorder:
    def __init__(self,device,duration):
        self.device=device;self.duration=min(150,max(1,duration));self.p=self.stream=None
        self.blocks=[];self.lock=threading.Lock();self.lifecycle=threading.RLock();self.first=None;self.flags=0;self.frames=0;self.cancel=threading.Event()
    def start(self):
        with self.lifecycle:
            if self.cancel.is_set():return
            self._start()
    def _start(self):
        from station_bridge.loopback import backend
        pa=backend();self.p=pa.PyAudio()
        d=self.p.get_device_info_by_index(self.device['index'])
        if d['name']!=self.device['name'] or d.get('isLoopbackDevice'):raise BridgeError('Микрофон изменился. Выберите его заново.')
        rate=self.device['rate'];channels=self.device['channels']
        def cb(raw,count,info,flags):
            now=time.monotonic()
            if self.cancel.is_set():return None,pa.paComplete
            age=info.get('current_time',0)-info.get('input_buffer_adc_time',0)
            start=now-age if math.isfinite(age) and 0<=age<2 else now-count/rate
            with self.lock:
                if self.first is None:self.first=start
                if self.frames+count>rate*self.duration:return None,pa.paComplete
                self.blocks.append(bytes(raw or b''));self.frames+=count;self.flags+=bool(flags)
            return None,pa.paContinue
        try:
            self.stream=self.p.open(format=pa.paInt16,channels=channels,rate=rate,input=True,
                input_device_index=self.device['index'],frames_per_buffer=max(128,rate//100),stream_callback=cb)
        except BaseException:self.close();raise
    def data(self):
        with self.lock:raw=b''.join(self.blocks);first=self.first;flags=self.flags
        arr=array.array('h');arr.frombytes(raw)
        channels=self.device['channels'];rate=self.device['rate']
        # Box-average decimation, not point-skipping; probe band remains < 2 kHz.
        step=max(1,round(rate/12000));values=[]
        for i in range(0,len(arr)-step*channels+1,step*channels):
            values.append(sum(arr[i+j*channels] for j in range(step))/step/32768)
        return values,rate/step,first,flags
    def close(self):
        with self.lifecycle:self._close()
    def _close(self):
        self.cancel.set()
        if self.stream:
            with contextlib.suppress(Exception):self.stream.stop_stream()
            with contextlib.suppress(Exception):self.stream.close()
            self.stream=None
        if self.p:
            with contextlib.suppress(Exception):self.p.terminate()
            self.p=None
    def erase(self):
        with self.lock:self.blocks.clear()


class Plan:
    def __init__(self,identities):
        self.ids=list(identities);self.base=None;self.rate=48000;self.trials=[];self.lock=threading.Lock()
        self.wave=probe(self.rate);self.duration=len(self.ids)*7.5+4
    def arm(self,frames):
        with self.lock:
            self.base=frames+round(self.rate*2)
            self.trials=[{'id':ident,'sample':self.base+round((n*7.5+r*2.5)*self.rate),'time':None}
                         for n,ident in enumerate(self.ids) for r in range(3)]
    def source(self,offset,frames,rate,channels):
        now=time.monotonic()
        with self.lock:
            for trial in self.trials:
                if offset<=trial['sample']<offset+frames and trial['time'] is None:
                    trial['time']=now+(trial['sample']-offset)/rate
        return bytes(frames*channels*2)
    def audio(self,ident,start,frames):
        vals=array.array('h',[0])*(frames*2)
        with self.lock:trials=[dict(t) for t in self.trials]
        for trial in trials:
            if trial['id']!=ident:continue
            a=max(start,trial['sample']);b=min(start+frames,trial['sample']+len(self.wave))
            for sample in range(a,b):
                v=int(self.wave[sample-trial['sample']]*32767);i=(sample-start)*2;vals[i]=vals[i+1]=v
        return vals.tobytes()


class ProbeProcessor:
    def __init__(self,capture,plan,ident):self.capture=capture;self.plan=plan;self.ident=ident
    def process_at(self,raw,cursor):
        return self.plan.audio(self.ident,cursor*self.capture.frames,len(raw)//(2*self.capture.channels))
    def process_samples(self,raw,source_sample):
        return self.plan.audio(self.ident,round(source_sample),len(raw)//(2*self.capture.channels))
    def reset(self):pass
    def stats(self):return {'calibration_probe':True}


def correlate(values,rate,start,emitted):
    ref=probe(round(rate));a=max(0,round((emitted-start-.030)*rate));b=min(len(values),round((emitted-start+1.8)*rate))
    x=values[a:b]
    if len(x)<len(ref):return None
    xa=(C.c_float*len(x))(*x);ra=(C.c_float*len(ref))(*ref);score=C.c_double()
    index=library().ysa_correlate(xa,len(x),ra,len(ref),C.byref(score))
    delay=(a+index)/rate+start-emitted
    if index<0 or score.value<.18 or not 0<=delay<=1.65:return None
    return {'delay_ms':delay*1000,'correlation':math.sqrt(max(0,min(1,score.value)))}


def summarize(plan,values,rate,origin,flags=0):
    result=[]
    for ident in plan.ids:
        vals=[]
        for t in plan.trials:
            if t['id']==ident and t['time'] is not None and origin is not None:
                value=correlate(values,rate,origin,t['time'])
                if value:vals.append(value)
        delays=[v['delay_ms'] for v in vals]
        result.append({'id':ident,'valid':len(delays)==3 and max(delays,default=0)-min(delays,default=0)<=25 and not flags,
            'matched':len(delays),'delay_ms':round(statistics.median(delays),2) if delays else None,
            'spread_ms':round(max(delays)-min(delays),2) if delays else None,
            'correlation':round(min((v['correlation'] for v in vals),default=0),3)})
    good=bool(result) and all(r['valid'] for r in result)
    if good:
        maximum=max(r['delay_ms'] for r in result)
        good=maximum-min(r['delay_ms'] for r in result)<=1000
        for r in result:r['correction_ms']=round(maximum-r['delay_ms'],2)
    return {'valid':good,'rows':result,'input_flags':flags,
            'note':'Относительный акустический замер; не room-EQ, не проверка фазы баса и не синхронизация часов Станций.'}


def signature(controller):
    data={'spatial':controller.spatial, 'pins':{p.device['id']:controller.settings.data.get('pins',{}).get(p.device['id']) for p in controller.all_peers()}}
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


async def run(controller,device):
    peers=controller.all_peers();plan=Plan([p.device['id'] for p in peers]);rec=Recorder(device,plan.duration+12)
    state=controller.room_state
    try:
        state.update(status='starting',progress=0,result=None)
        async with controller.action_lock:
            await controller.stop_playback()
            controller.room_plan=plan
            controller.calibration_until=time.monotonic()+180
            controller.media.signal_provider=plan.source
            await asyncio.to_thread(rec.start)
            # Always PCM10 for repeatable relative calibration. Windows output is not captured.
            await ControllerStart(controller)
        deadline=time.monotonic()+12
        while not all(p.media._live_tasks for p in controller.all_peers()):
            if time.monotonic()>deadline:raise BridgeError('Не все Станции запросили поток. Проверьте доступ каждой колонки до замера.')
            await asyncio.sleep(.15)
        plan.arm(controller.media.capture.written_frames)
        state['status']='measuring'
        started=time.monotonic()
        while time.monotonic()-started<plan.duration:
            state['progress']=min(99,round((time.monotonic()-started)/plan.duration*100))
            index=max(0,min(len(peers)-1,int((time.monotonic()-started-2)/7.5)))
            state['speaker']=peers[index].device['name']
            await asyncio.sleep(.15)
        await asyncio.to_thread(rec.close)
        state['status']='analyzing'
        values,rate,origin,flags=await asyncio.to_thread(rec.data)
        result=await asyncio.to_thread(summarize,plan,values,rate,origin,flags)
        values.clear();state.update(status='done',progress=100,result=result,signature=signature(controller))
    except asyncio.CancelledError:
        state.update(status='cancelled',progress=0)
        raise
    except Exception as e:
        state.update(status='error',error=str(e) if isinstance(e,BridgeError) else type(e).__name__)
    finally:
        await asyncio.to_thread(rec.close);rec.erase()
        async with controller.action_lock:
            await controller.stop_playback()
            controller.room_plan=None;controller.media.signal_provider=None;controller.calibration_until=0


async def ControllerStart(c):
    # Call base implementation so saved source selection does not enter a synthetic probe.
    from station_bridge.app import Controller
    return await Controller.start_live_playback(c,{'playback_mode':'main'},synthetic=True)
