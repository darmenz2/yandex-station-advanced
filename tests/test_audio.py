import array
import math
import shutil
import subprocess
import time
import wave
import pytest
from station_bridge.audio import HLSCapture, ffmpeg_executable, make_test_tone
from station_bridge.protocol import BridgeError


def test_wave_tone(tmp_path):
    target=tmp_path/'tone.wav'
    make_test_tone(target)
    with wave.open(str(target),'rb') as w:
        assert w.getframerate()==48000 and w.getnchannels()==2 and w.getsampwidth()==2
        assert w.getnframes()==144000
        data=array.array('h',w.readframes(w.getnframes()))
    assert max(map(abs,data)) <= 3600


def test_hls_synthetic_encode_and_decode(tmp_path):
    try: ffmpeg_executable()
    except BridgeError: pytest.skip('FFmpeg is not installed')
    capture=HLSCapture(tmp_path/'hls',synthetic=True)
    capture.start()
    try:
        for _ in range(120):
            if capture.ready(): break
            time.sleep(.1)
        assert capture.ready(), list(capture.stderr)
        manifest=(capture.directory/'live.m3u8').read_text()
        assert '#EXTM3U' in manifest and '#EXTINF:' in manifest
        entries=[line for line in manifest.splitlines() if line and not line.startswith('#')]
        assert len(entries)>=3
        for name in entries:
            path=capture.directory/name
            content=path.read_bytes()
            assert len(content)>188 and len(content)%188==0 and content[0]==0x47
        # Independent system decoder, if present. Not the encoder binary itself.
        decoder=shutil.which('ffmpeg')
        if decoder:
            proc=subprocess.run([decoder,'-v','error','-i',str(capture.directory/entries[0]),'-f','s16le','-acodec','pcm_s16le','-'],
                                 capture_output=True,timeout=10)
            assert proc.returncode==0,proc.stderr.decode(errors='replace')
            samples=array.array('h',proc.stdout)
            rms=math.sqrt(sum(v*v for v in samples)/len(samples))
            assert rms>100, 'encoded segment unexpectedly silent'
        assert capture.stats()['running']
    finally:
        capture.stop()
    assert capture.process.poll() is not None
    assert not capture.writer.is_alive()
    assert not capture.stderr_thread.is_alive()
