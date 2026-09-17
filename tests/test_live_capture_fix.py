"""1.0.4 regressions: no native Windows or physical Station is used here.

Fake PortAudio supplies callback PCM; FFmpeg/HLS, HTTP, meters and selectors
are real. Silence and endpoint changes are tested rather than assumed.
"""
from __future__ import annotations
import array
import asyncio
import json
import math
import shutil
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import aiohttp
import pytest
from aiohttp import web
from station_bridge import audio, loopback
from station_bridge.app import Controller, make_ui_app
from station_bridge.audio import HLSCapture, ffmpeg_executable
from station_bridge.loopback import SignalMeter, levels, resolve_loopback, enumerate_loopbacks, probe_outputs, recommend
from station_bridge.protocol import BridgeError
from test_servers import free_port


def tone(frames=960, channels=2, rate=48000):
    result = array.array('h')
    for i in range(frames):
        value = int(2800 * math.sin(2 * math.pi * 440 * i / rate))
        result.extend([value] * channels)
    if audio.os.sys.byteorder != 'little': result.byteswap()
    return result.tobytes()


def device(index, name, *, loop=True, channels=2, rate=48000):
    return {'index': index, 'name': name, 'hostApi': 2, 'isLoopbackDevice': loop,
            'maxInputChannels': channels, 'defaultSampleRate': rate}


class FakeStream:
    def __init__(self, owner, kwargs):
        self.owner, self.kwargs = owner, kwargs
        self.active = self.closed = False
        self.thread = None
        self.stop_event = threading.Event()
    def start_stream(self):
        if self.kwargs['input_device_index'] in self.owner.fail:
            raise OSError('test open failure')
        self.active = True
        def run():
            while not self.stop_event.is_set() and self.active:
                mode = self.owner.modes.get(self.kwargs['input_device_index'], 'none')
                frames = self.kwargs['frames_per_buffer']
                channels, rate = self.kwargs['channels'], self.kwargs['rate']
                if mode != 'none':
                    raw = (b'\x01' if mode == 'bad' else
                           tone(frames, channels, rate) if mode == 'tone' else
                           bytes(frames * channels * 2))
                    _, flag = self.kwargs['stream_callback'](raw, frames, {}, 0)
                    if flag != 0:
                        self.active = False
                self.stop_event.wait(frames / rate)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
    def is_active(self): return self.active
    def stop_stream(self):
        self.active = False; self.stop_event.set()
        if self.thread: self.thread.join(timeout=2)
    def close(self): self.stop_stream(); self.closed = True


class FakePA:
    def __init__(self, rows=None, default=21, modes=None, fail=()):
        self.rows = rows or [device(20, 'Monitor [Loopback]'), device(21, 'Headphones [Loopback]'),
                             device(3, 'Microphone', loop=False)]
        self.default = default
        self.modes = modes or {}
        self.fail = set(fail)
        self.streams = []
        self.terminated = False
    def __enter__(self): return self
    def __exit__(self, *args): self.terminate()
    def terminate(self): self.terminated = True
    def get_loopback_device_info_generator(self): return iter(self.rows)
    def get_default_wasapi_loopback(self):
        if self.default == -1: raise OSError('no default')
        return next(d for d in self.rows if d['index'] == self.default)
    def get_host_api_info_by_index(self, idx): return {'name': 'Windows WASAPI'}
    def open(self, **kwargs):
        assert kwargs['input'] is True
        assert kwargs['input_device_index'] in [d['index'] for d in self.rows if d['isLoopbackDevice']]
        stream = FakeStream(self, kwargs); self.streams.append(stream); return stream


def fake_backend(p):
    return SimpleNamespace(PyAudio=lambda: p, paInt16=8, paContinue=0, paComplete=1, paAbort=2)


@pytest.mark.parametrize('channels', [1, 2, 6, 8])
def test_pcm_levels_and_multichannel(channels):
    peak, rms = levels(tone(channels=channels))
    assert .08 < peak < .09 and .055 < rms < .065
    assert levels(bytes(80*channels)) == (0, 0)


def test_peak_hold_prevents_poll_from_missing_short_audio():
    clock=[100.0]; meter=SignalMeter(48000, 2, clock=lambda:clock[0])
    meter.feed(tone()); clock[0] += 1.5; meter.feed(bytes(3840))
    assert meter.snapshot()['input_level'] > .08
    clock[0] += .6
    assert meter.snapshot()['input_level'] == 0
    assert meter.snapshot()['peak_since_start'] > .08
    assert meter.snapshot()['last_signal_ago'] == 2.1


def test_meter_exact_frames_no_recording_export():
    meter=SignalMeter(48000, 2)
    meter.feed(tone(48000))
    meter.feed(bytes(48000 * 4), 1)
    stats=meter.snapshot()
    assert stats['received_frames'] == 96000
    assert stats['non_silent_seconds'] == 1
    assert stats['callback_count'] == 2 and stats['status_flags'] == 1
    assert not any(isinstance(v, (bytes, bytearray)) for v in stats.values())
    json.dumps(stats)


def test_malformed_pcm_rejected():
    with pytest.raises(ValueError): levels(b'1')
    with pytest.raises(ValueError): SignalMeter(48000,2).feed(b'12')


def test_no_audio_is_not_misreported_as_signal():
    meter=SignalMeter(48000,2); meter.feed(b''); meter.feed(bytes(3840))
    assert meter.snapshot()['last_signal_ago'] is None
    assert meter.snapshot()['input_level'] == 0


def test_selection_uses_current_default_not_first_endpoint():
    p=FakePA(); chosen=resolve_loopback(p)
    assert chosen['index'] == 21
    assert len(enumerate_loopbacks(p)) == 2


def test_reordered_indices_re_resolve_explicit_name():
    p=FakePA(rows=[device(20, 'Headphones [Loopback]'),device(21,'Monitor [Loopback]')])
    chosen=resolve_loopback(p, 21, {'index':21,'name':'Headphones [Loopback]','host_api':'Windows WASAPI'})
    assert chosen['index'] == 20 and 'Headphones' in chosen['name']


@pytest.mark.parametrize('selection', [
    {'name':'Disconnected'}, {'name':'Headphones [Loopback]','host_api':'DirectSound'},
])
def test_disappeared_or_changed_endpoint_never_silently_falls_back(selection):
    with pytest.raises(BridgeError): resolve_loopback(FakePA(),21,selection)


def test_duplicate_names_require_new_selection():
    p=FakePA(rows=[device(20,'Duplicate'), device(21,'Duplicate')])
    with pytest.raises(BridgeError): resolve_loopback(p, 21, {'name':'Duplicate'})


def test_no_default_requires_manual_not_first():
    with pytest.raises(BridgeError): resolve_loopback(FakePA(default=-1))
    assert resolve_loopback(FakePA(default=-1), 20)['index'] == 20


@pytest.mark.parametrize('channels,rate', [(0,48000),(9,48000),(2,0),(2,999999)])
def test_bad_capture_format(channels, rate):
    with pytest.raises(BridgeError):
        resolve_loopback(FakePA(rows=[device(21,'Bad',channels=channels,rate=rate)]),21)


def test_probe_only_loopbacks_finds_actual_signal_not_default(monkeypatch):
    p=FakePA(default=20,modes={20:'silence',21:'tone'})
    monkeypatch.setattr(loopback,'backend',lambda:fake_backend(p))
    result=probe_outputs(1)
    assert result['recommended']==21
    assert len(result['results'])==2 and 'Microphone' not in json.dumps(result)
    assert next(d for d in result['results'] if d['index']==20)['signal_detected'] is False
    assert next(d for d in result['results'] if d['index']==21)['signal_detected'] is True
    assert all(s.closed for s in p.streams) and p.terminated


def test_probe_with_no_signal_does_not_invent_choice(monkeypatch):
    p=FakePA(modes={20:'none',21:'silence'})
    monkeypatch.setattr(loopback,'backend',lambda:fake_backend(p))
    result=probe_outputs(1)
    assert result['recommended'] is None and result['recommended_key'] is None
    no_frames=next(r for r in result['results'] if r['index']==20)
    silence=next(r for r in result['results'] if r['index']==21)
    assert no_frames['callback_count']==0 and silence['callback_count']>0


def test_probe_closes_a_device_that_fails_to_start(monkeypatch):
    p=FakePA(fail={20},modes={21:'tone'})
    monkeypatch.setattr(loopback,'backend',lambda:fake_backend(p))
    result=probe_outputs(1)
    assert result['recommended']==21
    assert next(r for r in result['results'] if r['index']==20)['error']
    assert all(s.closed for s in p.streams)


@pytest.mark.parametrize('seconds', [0,-1,9,float('nan')])
def test_probe_duration_boundaries(seconds):
    with pytest.raises(BridgeError): probe_outputs(seconds)


def test_probe_limit_reports_skipped_outputs(monkeypatch):
    p=FakePA(rows=[device(i, str(i)) for i in range(30)],default=29)
    monkeypatch.setattr(loopback,'backend',lambda:fake_backend(p))
    result=probe_outputs(1)
    assert len(p.streams)==16 and len(result['results'])==30
    assert len([r for r in result['results'] if r['error']])==14
    assert any(s.kwargs['input_device_index']==29 for s in p.streams)


def test_invalid_probe_device_format_reports_error_not_crash(monkeypatch):
    p=FakePA(rows=[device(21,'bad',rate=0)])
    monkeypatch.setattr(loopback,'backend',lambda:fake_backend(p))
    result=probe_outputs(1)
    assert result['recommended'] is None and result['results'][0]['error']


def test_callback_error_is_exposed_not_hidden_by_ffmpeg_liveness(tmp_path,monkeypatch):
    p=FakePA(modes={21:'bad'})
    monkeypatch.setattr(audio,'backend',lambda:fake_backend(p))
    c=HLSCapture(tmp_path/'bad')
    c.start()
    try:
        for _ in range(50):
            if c.error: break
            time.sleep(.02)
        assert c.error and not c.stats()['running']
    finally:c.stop()
    assert p.terminated and all(s.closed for s in p.streams)


def test_actual_callback_pcm_reaches_real_hls_encoder_then_silence_resume(tmp_path, monkeypatch):
    p=FakePA(modes={21:'tone'})
    monkeypatch.setattr(audio,'backend',lambda:fake_backend(p))
    c=HLSCapture(tmp_path/'callback-hls',21,selection={'name':'Headphones [Loopback]'})
    c.start()
    try:
        for _ in range(100):
            if c.ready():break
            time.sleep(.1)
        assert c.ready(), list(c.stderr)
        stats=c.stats()
        assert stats['running'] and stats['device']['name']=='Headphones [Loopback]'
        assert stats['input_level']>.08 and stats['output_peak_since_start']>.08
        assert stats['non_silent_seconds']>1 and stats['output_non_silent_seconds']>1
        # Decode actual AAC/TS, not only check file creation.
        entries=[line for line in (c.directory/'live.m3u8').read_text().splitlines() if line and not line.startswith('#')]
        import subprocess
        decoder=shutil.which('ffmpeg')
        assert decoder, 'This integration check requires a separate FFmpeg decoder in PATH'
        decoded=subprocess.run([decoder,'-v','error','-i',str(c.directory/entries[1]),
                                '-f','s16le','-acodec','pcm_s16le','-'],capture_output=True,timeout=10)
        assert decoded.returncode==0 and levels(decoded.stdout)[1]>.02
        # The sender must say "no frames", not "network failure", on a paused source.
        p.modes[21]='none'; time.sleep(2.3)
        assert c.stats()['signal_status']=='no_frames'
        assert c.stats()['running'] and c.stats()['inserted_silence_seconds']>1
        p.modes[21]='tone'; time.sleep(.25)
        assert c.stats()['signal_status']=='signal'
        p.streams[0].stop_stream()
        assert c.stats()['capture_active'] is False and not c.stats()['running']
    finally:c.stop()


async def test_probe_api_is_explicit_and_protected(tmp_path,monkeypatch):
    c=Controller(tmp_path/'settings');await c.start()
    called=[]
    def fake_probe():
        called.append(True)
        return {'outputs':[{'index':3,'name':'Test output','key':'test'}], 'results':[],
                'recommended':3,'recommended_key':'test','duration':4}
    monkeypatch.setattr('station_bridge.app.probe_outputs',fake_probe)
    port=free_port(); runner=web.AppRunner(make_ui_app(c,'SECRET',port))
    await runner.setup();await web.TCPSite(runner,'127.0.0.1',port).start()
    base=f'http://127.0.0.1:{port}'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(base+'/api/action',json={'command':'probe_audio'}) as r:
                assert r.status==401 and not called
            async with session.post(base+'/api/action',json={'command':'probe_audio'},headers={'X-Bridge-Key':'SECRET'}) as r:
                assert r.status==200 and (await r.json())['recommended']==3
            async with session.get(base+'/api/diagnostic',headers={'X-Bridge-Key':'SECRET'}) as r:
                report=await r.json()
                assert report['audio_outputs'][0]['name']=='Test output'
                assert report['audio_probe']['recommended']==3
                assert 'SECRET' not in json.dumps(report)
        c.media.kind='live'
        with pytest.raises(BridgeError):await c.action('probe_audio',{})
        assert len(called)==1
    finally:
        await runner.cleanup();await c.close()


async def test_stale_frontend_key_cannot_start_on_wrong_device(tmp_path):
    c=Controller(tmp_path/'settings')
    c.glagol=SimpleNamespace(connected=True)
    c.outputs=[{'index':20,'name':'Other device','key':'new-key'}]
    with pytest.raises(BridgeError):await c.action('live',{'device_index':20,'device_key':'old-key'})
    assert not c.media.token and c.media.capture is None
    import shutil;shutil.rmtree(c.cache,ignore_errors=True)


async def test_hls_test_never_initializes_audio_input(tmp_path,monkeypatch):
    from station_bridge.media import MediaServer
    m=MediaServer(tmp_path/'cache',lambda text:None)
    await m.bind('127.0.0.1','127.0.0.1',free_port())
    def forbidden():raise AssertionError('HLS test must never touch a recording backend')
    monkeypatch.setattr(audio,'backend',forbidden)
    try:
        url=await m.start_live(None,1,synthetic=True)
        assert url.endswith('live.m3u8')
        assert m.capture.stats()['source']=='generated_test'
        assert m.capture.p is None and m.capture.stream is None
        assert m.capture.stats()['level']>.08
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as r:
                manifest=await r.text();assert r.status==200
            name=next(line for line in manifest.splitlines() if line and not line.startswith('#'))
            async with session.get(m.url(name)) as r:
                data=await r.read()
                assert r.status==200 and len(data)>188 and data[0]==0x47
        assert m.segment_requests==1
    finally:await m.close()
