"""Continuous source-driven PCM and an explicitly experimental clock servo.

The default path preserves consecutive source samples and has a one-time
startup reservoir. HTTP wake-ups are not used as an audio clock. The legacy
per-reader resampler is opt-in only and does not measure a Station DAC clock.
Only an actual ring-history overwrite requires a counted discontinuity.
"""
from __future__ import annotations
import asyncio
import array
import contextlib
import ctypes as C
import math
import socket
import time
from aiohttp import web
from station_bridge.pcm_stream import wav_header, MAX_RIFF_DATA
from .spatial import library

_TABLE = None

def table():
    global _TABLE
    if _TABLE is None:
        data=[]
        for phase in range(257):
            f=phase/256
            row=[]
            for k in range(16):
                x=k-7-f
                sinc=.96 if abs(x)<1e-12 else math.sin(math.pi*.96*x)/(math.pi*x)
                window=.5+.5*math.cos(math.pi*x/8) if abs(x)<=8 else 0
                row.append(sinc*window)
            s=sum(row);data.extend(v/s for v in row)
        _TABLE=(C.c_double*len(data))(*data)
    return _TABLE

class Timeline:
    """Async first-reader barrier, once per explicit group start; never a DAC clock."""
    def __init__(self, ring, frames, expected, target_ms=30., timeout=1.5):
        self.ring=ring;self.frames=frames;self.expected=set(expected);self.arrived=set()
        self.target_ms=target_ms;self.timeout=timeout;self.anchor=None;self.origin=None
        self.event=asyncio.Event();self.positions={};self.timer=None
    def release(self):
        if self.event.is_set():return
        units=max(1,math.ceil(self.target_ms/(self.ring.frame_seconds*1000)))
        self.anchor=self.ring.live_cursor(units)*self.frames
        self.origin=time.monotonic()
        self.event.set()
        if self.timer:self.timer.cancel()
    async def join(self, ident):
        if not self.event.is_set():
            self.arrived.add(ident)
            if self.timer is None:self.timer=asyncio.get_running_loop().call_later(self.timeout,self.release)
            if self.expected.issubset(self.arrived):self.release()
            await self.event.wait()
            return float(self.anchor),self.origin
        # Late/reconnected reader starts at a currently sending peer's PC cursor.
        now=time.monotonic()
        recent=[v for v,t in self.positions.values() if now-t<.1]
        pos=min(recent) if recent else self.ring.live_cursor(max(1,round(self.target_ms/(self.ring.frame_seconds*1000))))*self.frames
        return float(pos),now
    def cancel(self):
        if self.timer:self.timer.cancel()

    def snapshot(self):
        return {'expected':len(self.expected),'arrived':len(self.arrived),'released':self.event.is_set(),
                'target_queue_ms':self.target_ms,'common_source_cursor':self.anchor,
                'clock':'PC sample timeline; speaker DAC synchronization is not measured'}

class Servo:
    def __init__(self,target_ms,enabled=True):
        self.target=target_ms;self.enabled=enabled;self.filtered=target_ms
        self.ratio=1.;self.correction_samples=0.;self.max_ratio=1.
    def update(self,lag_ms,seconds):
        dt=max(.0001,min(.1,seconds));alpha=1-math.exp(-dt/2.)
        self.filtered+=(lag_ms-self.filtered)*alpha
        err=self.filtered-self.target
        desired=1.
        if self.enabled and abs(err)>12:
            desired+=max(-.002,min(.002,(err-math.copysign(12,err))*.000025))
        old=self.ratio
        self.ratio+=max(-.0005*dt,min(.0005*dt,desired-self.ratio))
        self.max_ratio=max(self.max_ratio,self.ratio)
        return old,self.ratio

class Reader:
    def __init__(self,ring,frames,rate,channels,cursor,target_ms,enabled=True):
        self.ring,self.frames,self.rate,self.channels=ring,frames,rate,channels
        self.cursor=float(cursor);self.servo=Servo(target_ms,enabled)
        self.discontinuities=0;self.skipped_samples=0;self.emitted=0
        self.last_output=None;self.bridge=False;self.last_lag_ms=0.
        self.fn=library().ysa_resample
        self.fn.argtypes=[C.c_void_p,C.c_uint32,C.c_void_p,C.c_uint32,C.c_uint32,
                          C.c_double,C.c_double,C.c_double,C.POINTER(C.c_double),C.POINTER(C.c_double)]
        self.fn.restype=C.c_int
        self.table=table()
    def request_live(self):
        # No seek and no URL replacement. Ask the same bounded servo to converge.
        self.servo.enabled=True
        self.servo.filtered=max(self.servo.filtered,self.last_lag_ms)
    def take(self,wanted):
        newest=self.ring.snapshot()['encoded_frames']*self.frames
        first=self.ring.live_cursor(2048)*self.frames
        if first>0 and self.cursor < first+8:
            previous=self.cursor
            self.cursor=max(first+8,newest-round(self.servo.target*self.rate/1000))
            if previous>=0 and self.cursor>previous:
                self.discontinuities+=1;self.skipped_samples+=round(self.cursor-previous);self.bridge=True
        lag=(newest-self.cursor)/self.rate*1000;self.last_lag_ms=max(0,lag)
        # Freeze controller if data is temporarily absent; never manufacture speed.
        if newest-self.cursor<16:return b''
        old,new=self.servo.update(lag,wanted/self.rate)
        wanted=min(wanted,max(0,int((newest-self.cursor-9)/max(old,new))))
        if wanted<=0:return b''
        start=max(0,int(self.cursor)-7);block=start//self.frames
        end=math.ceil(self.cursor+wanted*max(old,new))+9
        count=math.ceil(end/self.frames)-block
        read=self.ring.read(block,count)
        if read.skipped:return b''
        start_sample=block*self.frames
        # Near stream origin, pad history with the first complete sample-frame.
        before=max(0,7-int(self.cursor-start_sample))
        raw=(read.data[:self.channels*2]*before)+read.data
        offset=self.cursor-start_sample+before
        out=C.create_string_buffer(wanted*self.channels*2);ending=C.c_double()
        n=self.fn(raw,len(raw)//(self.channels*2),out,wanted,self.channels,offset,old,new,self.table,C.byref(ending))
        if n<0:raise ValueError('Invalid PCM interpolation dimensions')
        self.cursor=start_sample+ending.value-before
        self.servo.correction_samples+=(old+new-2)*.5*n
        data=out.raw[:n*self.channels*2]
        if data and self.bridge and self.last_output is not None:
            samples=array.array('h',data);length=min(n,max(1,round(.004*self.rate)))
            for i in range(length):
                w=(i+1)/length
                for c in range(self.channels):
                    samples[i*self.channels+c]=round(self.last_output[c]*(1-w)+samples[i*self.channels+c]*w)
            data=samples.tobytes();self.bridge=False
        if data:self.last_output=array.array('h',data[-self.channels*2:])
        self.emitted+=n
        return data
    def stats(self):
        return {'method':'sample_cursor_servo','pc_backlog_ms':round(self.last_lag_ms,2),
                'filtered_pc_backlog_ms':round(self.servo.filtered,2),'target_pc_ms':self.servo.target,
                'rate_correction_ppm':round((self.servo.ratio-1)*1e6,1),
                'corrected_source_samples':round(self.servo.correction_samples,2),
                'discontinuities':self.discontinuities,'skipped_source_samples':self.skipped_samples,
                'source_sample_cursor':round(self.cursor,3),'emitted_samples':self.emitted,
                'acoustic_offset_ms':None,'note':'PC queue correction only; does not measure or flush the speaker buffer'}

class FifoReader:
    """Read consecutive source frames; network scheduling never becomes an audio clock.

    A small startup reservoir is delivered before following callback arrivals. It is NOT an
    estimate of what the Station has played. No resampling, per-reader clock,
    or missing-data silence is introduced here. Only actual overwritten ring
    data requires a counted discontinuity.
    """
    def __init__(self, ring, frames, rate, channels, cursor, target_ms, enabled=False):
        self.ring, self.frames, self.rate, self.channels = ring, frames, rate, channels
        self.cursor = float(round(cursor))
        self.reserve_blocks = max(1, math.ceil(target_ms * rate / (1000 * frames)))
        self.target_ms = self.reserve_blocks * frames / rate * 1000
        self.discontinuities = self.skipped_samples = self.emitted = 0
        self.last_lag_ms = 0.0
        self.last_output = None
        self.waits = 0
        self.max_backlog_ms = 0.0
        self.bridge = False
        self.primed = False
        self.startup_payload_samples = 0

    def request_live(self):
        # Nothing to flush in a source-driven FIFO. The receiver's buffer is
        # not observable through HTTP; don't falsely promise to shrink it.
        return False

    def take(self, wanted):
        newest = self.ring.snapshot()['encoded_frames']
        first = self.ring.live_cursor(1_000_000)
        seq = int(self.cursor) // self.frames
        if seq < first:
            self.skipped_samples += (first-seq)*self.frames
            self.discontinuities += 1
            self.bridge = True
            seq = first
            self.cursor = float(seq*self.frames)
        self.last_lag_ms = max(0., (newest-seq)*self.frames/self.rate*1000)
        self.max_backlog_ms = max(self.max_backlog_ms, self.last_lag_ms)
        available = newest*self.frames-int(self.cursor)
        if not self.primed:
            if available < self.reserve_blocks*self.frames:
                self.waits += 1
                return b''
            self.primed = True
            self.startup_payload_samples = available
        # Supply the accumulated startup reserve, then follow source arrivals.
        # Holding the reserve permanently on the PC would not protect a remote
        # decoder from starvation while the HTTP loop briefly stalls.
        n = min(wanted, max(0, available))
        if not n:
            self.waits += 1
            return b''
        offset = int(self.cursor)-seq*self.frames
        blocks = math.ceil((offset+n)/self.frames)
        data = self.ring.read(seq, blocks)
        if data.skipped:
            # A producer raced the read. The next call records the loss once.
            return b''
        raw = data.data[offset*self.channels*2:(offset+n)*self.channels*2]
        self.cursor += len(raw)//(self.channels*2)
        self.emitted += len(raw)//(self.channels*2)
        if raw and self.bridge and self.last_output is not None:
            samples = array.array('h', raw)
            count = min(len(samples)//self.channels, max(1, round(.004*self.rate)))
            for n in range(count):
                w=(n+1)/count
                for c in range(self.channels):
                    j=n*self.channels+c
                    samples[j]=round(self.last_output[c]*(1-w)+samples[j]*w)
            raw=samples.tobytes()
            self.bridge=False
        if raw:
            self.last_output=array.array('h',raw[-self.channels*2:])
        return raw

    def stats(self):
        return {'method':'source_sample_fifo', 'pc_backlog_ms':round(self.last_lag_ms,2),
                'target_pc_ms':0.,'startup_reserve_ms':round(self.target_ms,2),
                'startup_reserve_sent_samples':self.startup_payload_samples, 'primed':self.primed,
                'rate_correction_ppm':0.,'corrected_source_samples':0.,
                'discontinuities':self.discontinuities,'skipped_source_samples':self.skipped_samples,
                'source_sample_cursor':round(self.cursor),'emitted_samples':self.emitted,
                'max_pc_backlog_ms':round(self.max_backlog_ms,2),'data_waits':self.waits,
                'acoustic_offset_ms':None,
                'note':'FIFO follows capture samples, not HTTP wake-up times. Receiver clock and acoustic delay are unknown.'}

async def handle(server,request,token,capture):
    ring=capture.live_ring;factory=getattr(server,'processor_factory',None)
    processor=factory(capture) if factory else None
    channels=2 if processor else capture.channels
    count=getattr(server,'finite_sample_budget',None) or min(capture.rate*21600,MAX_RIFF_DATA//(channels*2))
    headers={'Content-Type':'audio/wav','Content-Length':str(44+count*channels*2),'Cache-Control':'no-store',
             'Accept-Ranges':'none','X-Content-Type-Options':'nosniff','Connection':'close'}
    if request.method=='HEAD':server.head_requests+=1;return web.Response(headers=headers)
    # One live reader for this Station. A probe/range GET must not silently spawn a second DSP clock.
    if server._live_tasks:raise web.HTTPServiceUnavailable(headers={'Retry-After':'1'})
    response=web.StreamResponse(headers=headers);response.force_close()
    task=asyncio.current_task();server._live_tasks.add(task)
    subscription=ring.subscribe();server.requests+=1;server.live_connections+=1
    server.last_request=time.monotonic();server.last_processor=processor
    reader=None;sent=0;reason='stream_revoked';complete=False;server.waiting_for_group=True
    server.event('reader_open',active=1,continuous=True)
    try:
        transport=request.transport
        if transport:
            transport.set_write_buffer_limits(high=max(8192,capture.frames*channels*8),low=1024)
            sock=transport.get_extra_info('socket')
            if sock:
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
                    sock.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,16384)
                    server.live_socket_buffer_bytes=sock.getsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF)
        await response.prepare(request)
        async with asyncio.timeout(server.live_write_timeout):await response.write(wav_header(capture.rate,channels,count*channels*2))
        timeline=server.shared_timeline;ident=server.timeline_id
        cursor,next_tick=await timeline.join(ident)
        legacy_servo = bool(getattr(server, 'smooth_enabled', False))
        reader_type = Reader if legacy_servo else FifoReader
        reader=reader_type(ring,capture.frames,capture.rate,capture.channels,cursor,timeline.target_ms, legacy_servo)
        server.steady_reader=reader;server.waiting_for_group=False
        server.event('common_timeline_start',sample=round(cursor),expected=len(timeline.expected))
        last_data=time.monotonic();last_discontinuity=0
        while server.token==token and server.capture is capture and not capture.stop_event.is_set() and sent<count:
            delay=next_tick-time.monotonic()
            if legacy_servo and delay>0:await asyncio.sleep(delay)
            now=time.monotonic()
            if legacy_servo and now-next_tick>.040:
                # Legacy experimental mode only. Source FIFO does not pace by this timer.
                server.pacing_stalls+=1;next_tick=now
            subscription.clear()
            start_cursor=reader.cursor
            batch = capture.frames if legacy_servo else max(capture.frames, round(.040*capture.rate)//capture.frames*capture.frames)
            raw=reader.take(min(batch,count-sent))
            if not raw:
                if capture.error or now-last_data>3:reason='producer_no_data';break
                with contextlib.suppress(asyncio.TimeoutError):await subscription.wait(.1)
                continue
            n=len(raw)//(capture.channels*2)
            payload=(processor.process_samples(raw,start_cursor) if hasattr(processor,'process_samples')
                     else processor.process(raw) if processor else raw)
            if reader.discontinuities>last_discontinuity:
                # Do not reset the crossover/delay filters: that caused audible cold starts.
                server.event('concealed_source_gap',samples=reader.skipped_samples)
                last_discontinuity=reader.discontinuities
            begin=time.monotonic()
            async with asyncio.timeout(server.live_write_timeout):await response.write(payload)
            last_data=time.monotonic();elapsed=(last_data-begin)*1000
            server.live_max_write_ms=max(server.live_max_write_ms,elapsed)
            if elapsed>50:server.live_write_stalls+=1;server.event('write_stall',duration_ms=round(elapsed,1))
            server.last_request=last_data;server.live_bytes_sent+=len(payload);server.bytes_requested+=len(payload)
            server.live_send_lag_ms=round(reader.last_lag_ms,2)
            server.live_skipped_frames=reader.skipped_samples//capture.frames
            if transport:server.live_write_queue_bytes=transport.get_write_buffer_size()
            if server.first_audio_request_seconds is None:server.first_audio_request_seconds=round(last_data-server.started_at,3)
            timeline.positions[ident]=(reader.cursor,last_data)
            sent+=n;next_tick+=n/capture.rate
            # Yield between small batches without a timed audio sleep.
            if not legacy_servo:await asyncio.sleep(0)
        complete=sent==count
        if complete:reason='finite_response_complete';await response.write_eof()
    except asyncio.CancelledError:reason='cancelled_by_user';raise
    except asyncio.TimeoutError:reason='http_write_timeout'
    except (ConnectionError,OSError):reason='peer_disconnected'
    except Exception as exc:
        reason='pcm_processing_error';server.log('PCM: '+type(exc).__name__)
    finally:
        server.waiting_for_group=False;subscription.close();server._live_tasks.discard(task)
        server.last_close_reason=reason;server.last_close_at=time.monotonic()
        server.event('reader_close',reason=reason,samples=sent,active=0)
        if reader:server.last_steady_stats=reader.stats()
        server.steady_reader=None
        # A last subscriber must not poison the next connection with stale data.
        if not complete and request.transport:request.transport.abort()
    return response
