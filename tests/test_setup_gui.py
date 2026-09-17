"""Focused tests for the GUI/launch update. Not Windows hardware tests."""
from __future__ import annotations
import asyncio
import errno
import importlib.util
import json
from pathlib import Path
import socket
import struct
import zipfile
import hashlib

import pytest
from aiohttp import ClientSession, web
from advanced.listener import reserve_panel_socket
from station_bridge.app import make_ui_app

ROOT=Path(__file__).resolve().parent.parent

@pytest.mark.parametrize('value',[True,False,0,1023,65536,-1,'8779',1.0,None])
def test_invalid_port(value):
    with pytest.raises(ValueError):reserve_panel_socket(value)

@pytest.mark.parametrize('attempts',[True,0,17,-1,None])
def test_invalid_attempts(attempts):
    with pytest.raises(ValueError):reserve_panel_socket(8779,attempts)


def test_port_is_reserved_not_just_probed():
    first=reserve_panel_socket()
    try:
        addr=first.getsockname()
        assert addr[0]=='127.0.0.1'
        assert first.getblocking() is False
        with socket.socket() as other:
            with pytest.raises(OSError):other.bind(addr)
    finally:first.close()


def test_occupied_default_uses_another_port_without_touching_owner():
    first=reserve_panel_socket()
    try:
        port=first.getsockname()[1]
        second=reserve_panel_socket(port)
        try:
            assert second.getsockname()[1] != port
            assert first.fileno()>=0
            assert first.getsockname()[1]==port
        finally:second.close()
    finally:first.close()


def test_last_port_falls_back_to_ephemeral(monkeypatch):
    calls=[]
    class Fake:
        def setsockopt(self,*a):pass
        def bind(self,addr):
            calls.append(addr)
            if addr[1]:raise OSError(errno.EADDRINUSE,'busy')
        def listen(self,n):pass
        def setblocking(self,b):pass
        def close(self):pass
    monkeypatch.setattr(socket,'socket',lambda *a:Fake())
    reserve_panel_socket(65535)
    assert calls==[('127.0.0.1',65535),('127.0.0.1',0)]


def test_non_port_errors_are_not_hidden(monkeypatch):
    class Fake:
        def setsockopt(self,*a):pass
        def bind(self,a):raise OSError(errno.EMFILE,'too many files')
        def close(self):self.closed=True
    monkeypatch.setattr(socket,'socket',lambda *a:Fake())
    with pytest.raises(OSError) as info:reserve_panel_socket()
    assert info.value.errno==errno.EMFILE


def test_access_denied_reserved_windows_port_is_skipped(monkeypatch):
    calls=[]
    class Fake:
        def setsockopt(self,*a):pass
        def bind(self,addr):
            calls.append(addr[1])
            if len(calls)==1:raise OSError(errno.EACCES,'reserved range')
        def listen(self,n):pass
        def setblocking(self,b):pass
        def close(self):pass
    monkeypatch.setattr(socket,'socket',lambda *a:Fake())
    reserve_panel_socket(8779)
    assert calls==[8779,8780]


def test_runtime_boundary_uses_actual_reserved_port():
    async def run():
        class Controller:
            desktop_mode=True
            last_ui_seen=0
            def snapshot(self):return {'ok':True}
            def log(self,text):pass
        occupied=reserve_panel_socket()
        sock=reserve_panel_socket(occupied.getsockname()[1])
        port=sock.getsockname()[1]
        app=make_ui_app(Controller(),'unit-test-key',port)
        runner=web.AppRunner(app,access_log=None)
        try:
            await runner.setup();await web.SockSite(runner,sock).start()
            async with ClientSession() as session:
                url=f'http://127.0.0.1:{port}/api/state'
                async with session.get(url,headers={'X-Bridge-Key':'unit-test-key'}) as r:
                    assert r.status==200;assert await r.json()=={'ok':True}
                for headers,code in [({},401),({'X-Bridge-Key':'bad'},401),
                    ({'X-Bridge-Key':'unit-test-key','Origin':'https://example.com'},403),
                    ({'X-Bridge-Key':'unit-test-key','Host':'127.0.0.1:8769'},403)]:
                    async with session.get(url,headers=headers) as r:assert r.status==code
        finally:
            await runner.cleanup();sock.close();occupied.close()
    asyncio.run(run())


def test_install_does_not_launch_legacy_main():
    bootstrap=(ROOT/'bootstrap.ps1').read_text(encoding='utf-8-sig')
    run=(ROOT/'Run.cmd').read_text()
    script=(ROOT/'installer/GuiInstall.ps1').read_text(encoding='utf-8-sig')
    assert "'main.py'" not in bootstrap
    assert '-InstallOnly' in script
    assert '-InstallOnly' in run
    assert 'Start-Process' not in script
    assert 'CreateNoWindow=$true' in script
    assert 'CreateNoWindow=$true' in bootstrap
    assert '& $Python' not in bootstrap
    assert bootstrap.index('if ($InstallOnly)') < bootstrap.index("$DesktopEntry =")


def test_existing_runtime_not_removed_for_port_conflict():
    bootstrap=(ROOT/'bootstrap.ps1').read_text(encoding='utf-8-sig')
    assert 'Checking installed components' in bootstrap
    assert 'no downloads needed' in bootstrap
    assert "$NeedsInstall = ($CheckCode -ne 0)" in bootstrap
    assert '8769' not in bootstrap


def test_no_forced_process_termination_or_security_disable():
    text='\n'.join(p.read_text(encoding='utf-8-sig') for p in [ROOT/'bootstrap.ps1',ROOT/'installer/GuiInstall.ps1',ROOT/'installer/UninstallGui.ps1'])
    for forbidden in ['Stop-Process','taskkill','Set-NetFirewallProfile','Set-MpPreference','testsigning','Read-Host']:
        assert forbidden not in text
    assert 'Get-AuthenticodeSignature' in text
    assert 'Get-FileHash' in text


def test_settings_and_login_not_overwritten():
    text=(ROOT/'installer/GuiInstall.ps1').read_text(encoding='utf-8-sig')
    assert "$ImportLegacy -eq 1 -and -not (Test-Path (Join-Path $Data 'settings.json'))" in text
    assert "Remove-Item -LiteralPath $Data" not in text
    assert "Remove-Item -LiteralPath $old" not in text


def test_script_source_utf8_bom():
    for p in [ROOT/'bootstrap.ps1',*list((ROOT/'installer').glob('*.ps1'))]:
        assert p.read_bytes().startswith(b'\xef\xbb\xbf'),p


def test_payload_all_hashes_and_no_secrets():
    payload=ROOT/'dist/payload.zip'
    with zipfile.ZipFile(payload) as z:
        manifest=json.loads(z.read('PAYLOAD_MANIFEST.json'))
        names=set(z.namelist())
        assert manifest['version']=='2.0.0-preview.5'
        for entry in manifest['files']:
            assert hashlib.sha256(z.read(entry['path'])).hexdigest()==entry['sha256']
        assert set(e['path'] for e in manifest['files'])|{'PAYLOAD_MANIFEST.json'}==names
        assert 'installer/Install.ps1' not in names
        assert 'installer/Uninstall.ps1' not in names
        for name in names:
            assert not name.startswith(('.runtime/','tests/','build/'))
            assert Path(name).name not in ('account.dpapi','settings.json','desktop.log','install.log')
        assert 'YandexStationAdvanced.exe' in names
        assert 'Uninstall.exe' in names


def pe_info(path):
    raw=path.read_bytes();assert raw[:2]==b'MZ'
    pe=struct.unpack_from('<I',raw,0x3c)[0];assert raw[pe:pe+4]==b'PE\0\0'
    machine=struct.unpack_from('<H',raw,pe+4)[0]
    magic=struct.unpack_from('<H',raw,pe+24)[0]
    subsystem=struct.unpack_from('<H',raw,pe+24+68)[0]
    # PE32+ optional header offset 112: data directory 4 is Authenticode certificate.
    signature=struct.unpack_from('<II',raw,pe+24+112+4*8)
    return raw,machine,magic,subsystem,signature

@pytest.mark.parametrize('name',['YandexStationAdvanced.exe','Uninstall.exe','dist/YandexStationAdvanced-Setup-2.0.0-preview.5-GUI-x64.exe'])
def test_native_files_are_x64_gui_and_honestly_unsigned(name):
    raw,machine,magic,subsystem,signature=pe_info(ROOT/name)
    assert (machine,magic,subsystem)==(0x8664,0x20b,2)
    assert signature==(0,0)
    assert b'asInvoker' in raw
    assert b'Microsoft.Windows.Common-Controls' in raw


def test_generated_code_no_build_only_payload_leak():
    with zipfile.ZipFile(ROOT/'dist/payload.zip') as z:
        assert not any(n.endswith('payload.inc') for n in z.namelist())
        assert not any(n.endswith('.obj') or n.endswith('.lib') for n in z.namelist())


def test_desktop_entry_compiles_and_uses_socksite():
    source=(ROOT/'advanced_entry.py').read_text()
    compile(source,'advanced_entry.py','exec')
    assert 'web.SockSite(runner, listener)' in source
    assert 'port = listener.getsockname()[1]' in source
    assert 'make_ui_app(controller,secret,port)' in source
    assert 'listener.close()' in source
