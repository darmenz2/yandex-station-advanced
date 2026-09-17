"""Reproducible PE32+ GUI launcher build. Requires clang/lld-link; no SDK/driver."""
from pathlib import Path
import struct,subprocess
SRC=Path(__file__).resolve().parent
OUT=SRC.parent/'Launch_PCM_106.exe'
BUILD=SRC/'build';BUILD.mkdir(exist_ok=True)
IMPORTS={'kernel32':['GetModuleFileNameW','GetEnvironmentVariableW','GetFileAttributesW','OpenMutexW','CloseHandle','ExitProcess','lstrlenW','CreateProcessW'],
         'user32':['MessageBoxW'],'shell32':['SHBrowseForFolderW','SHGetPathFromIDListW'],
         'ole32':['CoInitializeEx','CoUninitialize','CoTaskMemFree']}
for name,exports in IMPORTS.items():
    deffile=BUILD/(name+'.def');deffile.write_text('LIBRARY '+name+'.dll\nEXPORTS\n'+'\n'.join(exports)+'\n')
    subprocess.run(['lld-link','/lib','/machine:x64','/def:'+str(deffile),'/out:'+str(BUILD/(name+'.lib'))],check=True)

def resource(t,n,b,lang=0x419):
    return struct.pack('<IIHHHHIHHII',len(b),32,0xffff,t,0xffff,n,0,0x1030,lang,0,0)+b+b'\0'*(-len(b)%4)
manifest=b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
 <assemblyIdentity version="1.0.6.1" processorArchitecture="amd64" name="StationBridgePCM106Recovery" type="win32"/>
 <description>Separate recovery launcher for unchanged PCM 1.0.6</description>
 <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges></security></trustInfo>
</assembly>'''
res=resource(0,0,b'',0)+resource(24,1,manifest,0)
ico=(SRC/'app.ico').read_bytes();_,kind,count=struct.unpack_from('<HHH',ico);assert kind==1
group=struct.pack('<HHH',0,1,count)
for i in range(count):
    vals=struct.unpack_from('<BBBBHHII',ico,6+16*i)
    res+=resource(3,10+i,ico[vals[7]:vals[7]+vals[6]])
    group+=struct.pack('<BBBBHHIH',*vals[:7],10+i)
res+=resource(14,1,group)
(BUILD/'app.res').write_bytes(res)
subprocess.run(['clang','--target=x86_64-pc-windows-msvc','-fno-stack-protector','-ffreestanding','-fno-builtin','-Os','-Wall','-Wextra','-Wno-unused-function','-c',str(SRC/'launch.c'),'-o',str(BUILD/'launch.obj')],check=True)
subprocess.run(['lld-link','/entry:entry','/subsystem:windows,6.02','/machine:x64','/nodefaultlib','/manifest:no','/dynamicbase','/nxcompat','/highentropyva','/timestamp:0','/out:'+str(OUT),str(BUILD/'launch.obj'),str(BUILD/'app.res'),*(str(BUILD/(n+'.lib')) for n in IMPORTS)],check=True)
print(OUT)
