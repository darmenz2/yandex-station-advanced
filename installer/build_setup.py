"""Build Windows x64 GUI executables with LLVM, without redistributing an SDK.

Requires Python 3, clang and lld-link. No network is used during the build.
The embedded app is source-only except for this project's own launcher/uninstaller.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parent.parent
BUILD=ROOT/'build'
DIST=ROOT/'dist'
VERSION='2.0.0-preview.5'
IMPORTS={
 'kernel32': '''GetModuleHandleW GetModuleFileNameW GetCommandLineW GetEnvironmentVariableW
 GetLastError ExitProcess GetCurrentProcessId GetTempPathW GetTempFileNameW
 CreateDirectoryW RemoveDirectoryW DeleteFileW CopyFileW GetFileAttributesW
 CreateFileW ReadFile WriteFile GetFileSize SetFilePointerEx CloseHandle CreateProcessW
 GetExitCodeProcess WaitForSingleObject CreateMutexW OpenMutexW LocalAlloc LocalFree
 MultiByteToWideChar lstrlenW lstrcmpW lstrcmpiW FindFirstFileW FindNextFileW FindClose'''.split(),
 'user32': '''RegisterClassW CreateWindowExW DefWindowProcW DestroyWindow ShowWindow
 UpdateWindow EnableWindow GetMessageW TranslateMessage DispatchMessageW IsDialogMessageW
 PostQuitMessage SendMessageW SetWindowTextW GetWindowTextW SetFocus MessageBoxW
 LoadIconW LoadCursorW GetSystemMetrics AdjustWindowRectEx GetDC ReleaseDC SetTimer KillTimer'''.split(),
 'gdi32': '''SetBkMode SetTextColor SetBkColor CreateSolidBrush DeleteObject GetDeviceCaps CreateFontW'''.split(),
 'comctl32':['InitCommonControlsEx'],
 'shell32':'''ShellExecuteW CommandLineToArgvW SHBrowseForFolderW SHGetPathFromIDListW SHGetFolderPathW'''.split(),
 'ole32':['CoInitializeEx','CoUninitialize','CoTaskMemFree'],
}
MANIFEST='''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
 <assemblyIdentity version="2.0.0.5" processorArchitecture="amd64" name="YandexStationAdvanced" type="win32"/>
 <description>Yandex Station Advanced (unofficial project)</description>
 <dependency><dependentAssembly><assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" version="6.0.0.0" processorArchitecture="amd64" publicKeyToken="6595b64144ccf1df" language="*"/></dependentAssembly></dependency>
 <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges></security></trustInfo>
 <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1"><application><supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/></application></compatibility>
 <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings><dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true</dpiAware></windowsSettings></application>
</assembly>'''

def pad(data: bytes)->bytes:
    return data+b'\0'*(-len(data)%4)

def resource(typ:int,name:int,data:bytes,lang:int=0x419)->bytes:
    identifiers=struct.pack('<HHHH',0xffff,typ,0xffff,name)
    tail=struct.pack('<IHHII',0,0x1030,lang,0,0)
    header=struct.pack('<II',len(data),32)+identifiers+tail
    return header+pad(data)

def vblock(key:str,value:bytes=b'',children:bytes=b'',text:bool=False)->bytes:
    body=pad(b'\0'*6+(key+'\0').encode('utf-16le'))+value
    if children:body=pad(body)+children
    return struct.pack('<HHH',len(body),len(value)//2 if text else len(value),1 if text else 0)+body[6:]

def res_file(description:str)->bytes:
    null=resource(0,0,b'',lang=0)
    ico=(ROOT/'assets/app.ico').read_bytes();_,kind,count=struct.unpack_from('<HHH',ico)
    assert kind==1 and count
    group=struct.pack('<HHH',0,1,count);res=null
    for i in range(count):
        vals=struct.unpack_from('<BBBBHHII',ico,6+16*i)
        data=ico[vals[7]:vals[7]+vals[6]]
        res+=resource(3,10+i,data)
        group+=struct.pack('<BBBBHHIH',*vals[:7],10+i)
    res+=resource(14,1,group)
    fields={'CompanyName':'Yandex Station Advanced project (unofficial)', 'FileDescription':description,
            'FileVersion':VERSION,'ProductName':'Yandex Station Advanced','ProductVersion':VERSION,
            'LegalCopyright':'MIT license; see included notices'}
    children=b''.join(pad(vblock(k,(v+'\0').encode('utf-16le'),text=True)) for k,v in fields.items())
    strings=vblock('StringFileInfo',children=vblock('041904B0',children=children,text=True),text=True)
    var=vblock('VarFileInfo',children=vblock('Translation',struct.pack('<HH',0x419,1200)),text=True)
    fixed=struct.pack('<13I',0xFEEF04BD,0x10000,0x20000,5,0x20000,5,0x3f,0,0x40004,1,0,0,0)
    res+=resource(16,1,vblock('VS_VERSION_INFO',fixed,pad(strings)+pad(var)))
    return res

def run(*args):
    p=subprocess.run([str(a) for a in args],text=True,capture_output=True)
    if p.stdout:print(p.stdout)
    if p.stderr:print(p.stderr,file=sys.stderr)
    if p.returncode:raise RuntimeError('Build command failed: '+' '.join(map(str,args)))

def tools():
    BUILD.mkdir(exist_ok=True);DIST.mkdir(exist_ok=True)
    for name,exports in IMPORTS.items():
        definition=BUILD/(name+'.def')
        definition.write_text('LIBRARY '+name+'.dll\nEXPORTS\n'+'\n'.join(exports)+'\n')
        run('lld-link','/lib','/machine:x64','/def:'+str(definition),'/out:'+str(BUILD/(name+'.lib')))

def compile_exe(src:str,out:Path,description:str,defines=()):
    obj=BUILD/(out.stem+'.obj');res=BUILD/(out.stem+'.res')
    res.write_bytes(res_file(description))
    run('clang','--target=x86_64-pc-windows-msvc','-fno-stack-protector','-ffreestanding','-fno-builtin',
        '-Os','-Wall','-Wextra','-Wno-unused-function','-Wno-unused-variable',
        '-I'+str(BUILD),*(('-D'+d) for d in defines),'-c',ROOT/'installer'/src,'-o',obj)
    # Build manifest directly as RT_MANIFEST: no SDK resource compiler is needed.
    with res.open('ab') as f:f.write(resource(24,1,MANIFEST.encode('utf-8'),lang=0))
    run('lld-link','/entry:entry','/subsystem:windows,6.02','/machine:x64','/nodefaultlib','/manifest:no',
        '/dynamicbase','/nxcompat','/highentropyva','/timestamp:0','/out:'+str(out),obj,res,
        *(BUILD/(n+'.lib') for n in IMPORTS))

def collect()->list[Path]:
    names=['advanced','station_bridge','web','assets','licenses','installer']
    result=[]
    for name in names:
        for p in (ROOT/name).rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.inc','.obj','.so','.lib','.exp') and not p.name.endswith('_dev.py'):
                result.append(p)
    for name in ['advanced_entry.py','desktop_launcher.pyw','main.py','bootstrap.ps1','dependency_check.py',
                 'requirements.txt','Run.cmd','LICENSE','README.md','YandexStationAdvanced.exe','Uninstall.exe']:
        result.append(ROOT/name)
    return sorted(set(result))

def make_payload()->Path:
    entries=collect()
    manifest={'product':'YandexStationAdvanced','version':VERSION,'files':[
        {'path':p.relative_to(ROOT).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in entries]}
    m=json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8')
    path=DIST/'payload.zip'
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for p in entries:z.writestr(p.relative_to(ROOT).as_posix(),p.read_bytes())
        z.writestr('PAYLOAD_MANIFEST.json',m)
    return path

def inc(name:str,raw:bytes)->str:
    return 'static const unsigned char '+name+'[] = {\n'+','.join(str(x) for x in raw)+'\n};\n'

def main():
    from build_dsp import build
    build()
    tools()
    compile_exe('launcher.c',ROOT/'YandexStationAdvanced.exe','Yandex Station Advanced launcher')
    compile_exe('setup_gui.c',ROOT/'Uninstall.exe','Yandex Station Advanced uninstaller',('REMOVE_ONLY',))
    payload=make_payload()
    backend=(ROOT/'installer/GuiInstall.ps1').read_text(encoding='utf-8-sig')
    backend=backend.replace('__PAYLOAD_SHA256__',hashlib.sha256(payload.read_bytes()).hexdigest())
    # Windows PowerShell 5.1 requires a BOM for Unicode source files.
    script=b'\xef\xbb\xbf'+backend.encode('utf-8')
    (BUILD/'payload.inc').write_text(inc('embedded_payload',payload.read_bytes())+inc('embedded_script',script))
    exe=DIST/f'YandexStationAdvanced-Setup-{VERSION}-GUI-x64.exe'
    compile_exe('setup_gui.c',exe,'Yandex Station Advanced graphical installer')
    report={p.name:{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in [exe,payload,ROOT/'YandexStationAdvanced.exe',ROOT/'Uninstall.exe']}
    (DIST/'BUILD.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
