import asyncio
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit,parse_qs
import pytest

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
import recovery_start as r

def test_engine_is_original():
    r.validate_original_files()
    assert len(json.loads((ROOT/'ORIGINAL_ENGINE_MANIFEST.json').read_text())['unchanged']) == 21

def test_modified_file_is_rejected(tmp_path):
    (tmp_path/'bad.py').write_text('changed')
    (tmp_path/'ORIGINAL_ENGINE_MANIFEST.json').write_text(json.dumps({'unchanged':[{'path':'bad.py','sha256':'not-valid'}]}))
    with pytest.raises(RuntimeError,match='Изменён'):r.validate_original_files(tmp_path)

def test_manifest_cannot_traverse(tmp_path):
    (tmp_path/'ORIGINAL_ENGINE_MANIFEST.json').write_text(json.dumps({'unchanged':[{'path':'../outside','sha256':'no'}]}))
    with pytest.raises(RuntimeError,match='Недопустимый'):r.validate_original_files(tmp_path)

def test_only_connection_settings_copied(tmp_path):
    src=tmp_path/'Advanced';dst=tmp_path/'Recovery';src.mkdir()
    source={'remember':True,'pins':{'fixture-id':'fixture-pin'},'audio_peer_approvals':{'fixture-id':{'peer':'10.0.0.1'}},'device':{'name':'Fixture station'},'spatial':{'enabled':True},'desktop_group':[1,2,3],'smooth_sync_enabled':True}
    (src/'settings.json').write_text(json.dumps(source))
    (src/'account.dpapi').write_bytes(b'OPAQUE-FAKE-DPAPI-TEST')
    before={p.name:p.read_bytes() for p in src.iterdir()}
    assert r.copy_settings(src,dst)
    output=json.loads((dst/'settings.json').read_text())
    assert output['pins']==source['pins'] and output['remember'] is True
    assert 'spatial' not in output and 'desktop_group' not in output
    assert (dst/'account.dpapi').read_bytes()==before['account.dpapi']
    assert before=={p.name:p.read_bytes() for p in src.iterdir()}
    assert r.copy_settings(src,dst) is False

def test_no_copy_to_source(tmp_path):
    with pytest.raises(ValueError):r.copy_settings(tmp_path,tmp_path)

def test_no_overwrite_orphan_credential(tmp_path):
    src=tmp_path/'a';dst=tmp_path/'b';src.mkdir();dst.mkdir()
    (src/'settings.json').write_text('{"remember":true}')
    (src/'account.dpapi').write_bytes(b'new')
    (dst/'account.dpapi').write_bytes(b'keep')
    with pytest.raises(ValueError):r.copy_settings(src,dst)
    assert (dst/'account.dpapi').read_bytes()==b'keep'

def test_import_declined(tmp_path,monkeypatch):
    src=tmp_path/'YandexStationAdvanced';src.mkdir()
    (src/'settings.json').write_text('{"remember":true}')
    dst=tmp_path/'recovery';dst.mkdir()
    monkeypatch.setattr(r,'message',lambda *args:7)
    r.offer_import(tmp_path,dst)
    assert not (dst/'settings.json').exists()
    assert (dst/'first_start.done').exists()

def test_log_removes_private_url():
    log=io.StringIO();out=r.PrivateLog(log)
    out.write('URL http://127.0.0.1:8789/#key=TEST_SECRET\n')
    assert 'TEST_SECRET' not in log.getvalue()

def test_pe_gui():
    import struct
    b=(ROOT/'Launch_PCM_106.exe').read_bytes();assert b[:2]==b'MZ'
    p=struct.unpack_from('<I',b,0x3c)[0];assert b[p:p+4]==b'PE\0\0'
    assert struct.unpack_from('<H',b,p+4)[0]==0x8664
    assert struct.unpack_from('<H',b,p+24)[0]==0x20b
    assert struct.unpack_from('<H',b,p+24+68)[0]==2

def test_application_module_not_advanced():
    import station_bridge
    assert station_bridge.__version__=='1.0.6'
    assert Path(station_bridge.__file__).resolve().is_relative_to(ROOT)

@pytest.mark.asyncio
async def test_real_http_launch_and_stop(tmp_path,monkeypatch):
    import aiohttp,webbrowser
    from station_bridge import app
    monkeypatch.setattr(app,'audio_devices',lambda:[])
    monkeypatch.setattr(app,'interfaces',lambda:[])
    urls=[]
    monkeypatch.setattr(webbrowser,'open',lambda url:urls.append(url) or True)
    task=asyncio.create_task(r.run_recovery(tmp_path))
    try:
        for _ in range(100):
            if urls:break
            if task.done():await task
            await asyncio.sleep(.02)
        assert urls
        parsed=urlsplit(urls[0]);key=parse_qs(parsed.fragment)['key'][0]
        url=f'{parsed.scheme}://{parsed.netloc}'
        async with aiohttp.ClientSession() as session:
            async with session.get(url+'/api/state') as resp:
                assert resp.status in (401,403)
            async with session.get(url+'/api/state',headers={'X-Bridge-Key':key}) as resp:
                assert resp.status==200
                result=await resp.json();assert result['version']=='1.0.6'
                assert not result['media']['active']
            async with session.post(url+'/api/action',headers={'X-Bridge-Key':key,'Origin':url},json={'command':'shutdown'}) as resp:
                assert resp.status==200
        await asyncio.wait_for(task,5)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
