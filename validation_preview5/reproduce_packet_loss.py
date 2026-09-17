"""Compare old/new handling of one delayed but valid PCM packet (fake WASAPI)."""
from pathlib import Path
import subprocess, json, sys
ROOT=Path(__file__).resolve().parents[1]
CODE=r'''
from pathlib import Path
import json, time, tempfile
from station_bridge import audio
from test_live_capture_fix import FakePA, device, fake_backend
p=FakePA(rows=[device(21,'Manual [Loopback]')],modes={21:'none'})
audio.backend=lambda:fake_backend(p)
with tempfile.TemporaryDirectory() as td:
 c=audio.HLSCapture(Path(td),21,transport='pcm',latency_profile='fast');c.continuous_pcm=True;c.start()
 try:
  raw=b'\x23\x01'*960
  c.queue.put((time.monotonic()-.150,raw))
  deadline=time.monotonic()+.045
  while time.monotonic()<deadline and not(c.stale_frames or c.written_frames):time.sleep(.001)
  data=c.pcm_ring.read(0,4).data
  print(json.dumps({'valid_packet_ms':10,'delivery_delay_ms':150,'stale_samples':c.stale_frames,
                   'valid_packet_preserved':raw in data,'dropped_blocks':c.dropped,
                   'inserted_silence_samples':c.silence_frames}))
 finally:c.stop()
'''
def run(folder):
 import os
 env=dict(os.environ,PYTHONPATH=str(folder)+':'+str(folder/'tests'))
 return json.loads(subprocess.check_output([sys.executable,'-c',CODE],cwd=folder,env=env,text=True))
if __name__=='__main__':
 if len(sys.argv)!=2:raise SystemExit('Usage: python reproduce_packet_loss.py /path/to/preview4/source')
 result={'preview4':run(Path(sys.argv[1]).resolve()),'preview5':run(ROOT),'source':'simulated PortAudio; real receive-driven writer'}
 (ROOT/'validation_preview5/packet_loss_comparison.json').write_text(json.dumps(result,indent=2))
 print(json.dumps(result,indent=2))
