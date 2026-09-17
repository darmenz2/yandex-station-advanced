"""Real HTTP/DSP, FAKE PortAudio. Deliberate callback, loop and client stalls.

Measures stream integrity against the generated source, not acoustic latency.
No microphone, native Windows driver, home LAN or physical Yandex hardware.
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import array, asyncio, hashlib, json, math, threading, time
import aiohttp
from advanced.group import BorrowedMedia
from advanced.spatial import Processor
from advanced.reliable import setup_config
from advanced.steady_stream import Timeline
from station_bridge import audio
from station_bridge.media import MediaServer
from test_live_capture_fix import FakePA, device, fake_backend
import tempfile

DURATION=float(sys.argv[1]) if len(sys.argv)>1 else 180.
RATE=48000;FRAMES=480


def signal(start,n):
    a=array.array('h')
    for j in range(start,start+n):
        bass=3500*math.sin(2*math.pi*110*j/RATE)
        a.extend((round(bass+4700*math.sin(2*math.pi*440*j/RATE)),round(bass+5100*math.sin(2*math.pi*660*j/RATE))))
    return a.tobytes()


async def main():
    started=time.monotonic();tmp=Path(tempfile.mkdtemp(prefix='ysa-impair-'))
    p=FakePA(rows=[device(21,'Simulated Windows output [Loopback]')],modes={21:'none'})
    audio.backend=lambda:fake_backend(p)
    owner=MediaServer(tmp/'owner',lambda s:None);owner.continuous_pcm=True
    await owner.bind('127.0.0.1','127.0.0.1',0)
    capture_data=bytearray();halt=threading.Event();feed_info={'deliberate_callback_stalls':0,'callback_max_batch_ms':0}
    orig_start=audio.HLSCapture.start
    def start(cap):
        orig_start(cap)
        call=p.streams[0].kwargs['stream_callback']
        def feed():
            origin=time.monotonic();written=0;next_stall=1.6
            while not halt.is_set():
                t=time.monotonic()-origin
                if t>next_stall:
                    halt.wait(.110);next_stall+=6.;feed_info['deliberate_callback_stalls']+=1
                target=int((time.monotonic()-origin)*RATE)//FRAMES*FRAMES
                n=min(24*FRAMES,target-written)
                if n>0:
                    raw=signal(written,n);capture_data.extend(raw);written+=n
                    call(raw,n,{},0)
                    feed_info['callback_max_batch_ms']=max(feed_info['callback_max_batch_ms'],n/RATE*1000)
                else:halt.wait(.002)
        cap.validation_thread=threading.Thread(target=feed,daemon=True);cap.validation_thread.start()
    audio.HLSCapture.start=start
    ids=('left','right','bass');servers=[owner]
    try:
        await owner.start_live(21,1,transport='pcm',latency_profile='fast')
        for i in range(1,3):
            child=BorrowedMedia(tmp/f'peer{i}',lambda s:None);await child.bind('127.0.0.1','127.0.0.1',0);await child.attach(owner);servers.append(child)
        timeline=Timeline(owner.capture.live_ring,FRAMES,ids,target_ms=120)
        ds=[{'id':i,'name':i} for i in ids]
        spatial=setup_config({'mode':'midbass','roles':{'left':'left','right':'right','bass':'bass'}},ds)
        for s,ident in zip(servers,ids):
            s.port=next(iter(s.runner.sites))._server.sockets[0].getsockname()[1]
            s.shared_timeline=timeline;s.timeline_id=ident;s.smooth_enabled=False
            s.finite_sample_budget=round(DURATION*RATE)
            s.processor_factory=lambda cap,ident=ident:Processor(cap.rate,cap.channels,spatial,ident)
        loop_stalls={'count':0}; stop_async=asyncio.Event()
        async def obstruct_loop():
            while not stop_async.is_set():
                try:await asyncio.wait_for(stop_async.wait(),5.2)
                except asyncio.TimeoutError:
                    time.sleep(.065);loop_stalls['count']+=1
        staller=asyncio.create_task(obstruct_loop())
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=DURATION+30)) as session:
            async def read(s,index):
                async with session.get(s.current_url()) as response:
                    assert response.status==200
                    data=bytearray();mark=256000*(index+1);pauses=0
                    async for chunk in response.content.iter_chunked(16381):
                        data.extend(chunk)
                        if index and len(data)>mark:
                            await asyncio.sleep(.08+.02*index);mark+=256000*(index+1);pauses+=1
                    return data,pauses
            received=await asyncio.gather(*(read(s,i) for i,s in enumerate(servers)))
        stop_async.set();await staller;halt.set();owner.capture.validation_thread.join(2)
        anchor=round(timeline.anchor);count=round(DURATION*RATE)
        reference=memoryview(capture_data)[anchor*4:(anchor+count)*4]
        report={'duration_audio_seconds':DURATION,'elapsed_wall_seconds':round(time.monotonic()-started,3),
                'source':'Fake PortAudio callback using real receive-driven writer',
                'network':'real localhost TCP/HTTP; deliberate event-loop and consumer stalls',
                'not_tested':['Windows WASAPI','Yandex firmware','speaker acoustic delay','Wi-Fi'],
                'source_samples':len(capture_data)//4,'first_source_sample':anchor,
                'source_stats':owner.capture.stats(),'callback_impairment':feed_info,'event_loop_stalls':loop_stalls['count'],'readers':[]}
        for ident,s,(data,pauses) in zip(ids,servers,received):
            proc=Processor(RATE,2,spatial,ident);expected=bytearray()
            for offset in range(0,len(reference),4800*4):expected.extend(proc.process(reference[offset:offset+4800*4].tobytes()))
            real=data[44:]
            result={'role':ident,'http_bytes':len(data),'expected_bytes':count*4+44,
                    'matches_reference_pcm':real==expected,'sha256_audio':hashlib.sha256(real).hexdigest(),
                    'client_read_pauses':pauses,'close_reason':s.last_close_reason,
                    'reader':s.last_steady_stats,'http_max_write_ms':s.live_max_write_ms,
                    'stream_events':list(s.stream_events)}
            report['readers'].append(result)
        report['pass']=all(r['matches_reference_pcm'] and r['close_reason']=='finite_response_complete' and r['reader']['discontinuities']==0 for r in report['readers'])
        path=Path(__file__).with_name('impaired_soak.json');path.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
        print(json.dumps({'pass':report['pass'],'elapsed':report['elapsed_wall_seconds'],'source_stale':report['source_stats']['stale_pcm_seconds'],'inserted_silence':report['source_stats']['inserted_silence_seconds'],'stalls':loop_stalls,'roles':[(r['role'],r['matches_reference_pcm']) for r in report['readers']]},ensure_ascii=False),flush=True)
        if not report['pass']:raise SystemExit(1)
    finally:
        halt.set()
        for s in reversed(servers):await s.close()
        if 'timeline' in locals():timeline.cancel()

asyncio.run(main())
