from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parent.parent
p=ROOT/'advanced/native'
def build():
 src=p/'spatial_dsp.c'
 subprocess.run(['clang','-shared','-fPIC','-O2',str(src),'-o',str(p/'spatial_dsp.so')],check=True)
 subprocess.run(['clang','--target=x86_64-pc-windows-msvc','-ffreestanding','-fno-builtin','-fno-stack-protector','-O2','-c',str(src),'-o',str(p/'spatial_dsp.obj')],check=True)
 subprocess.run(['lld-link','/dll','/noentry','/nodefaultlib','/machine:x64','/dynamicbase','/nxcompat','/timestamp:0','/out:'+str(p/'spatial_dsp.dll'),str(p/'spatial_dsp.obj')],check=True)
if __name__=='__main__':build()
