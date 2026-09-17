"""Explicit user-requested Windows shell actions. No silent driver installation."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
from station_bridge.protocol import BridgeError

ROOT = Path(__file__).resolve().parent.parent

def shell_open(file, arguments='', verb='open'):
    if os.name != 'nt':
        raise BridgeError('Эта команда доступна только в Windows.')
    sh = ctypes.WinDLL('shell32', use_last_error=True)
    sh.ShellExecuteW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_int]
    sh.ShellExecuteW.restype=ctypes.c_void_p
    result=sh.ShellExecuteW(None, verb, str(file), arguments, str(ROOT), 1)
    if not result or result <= 32:
        raise BridgeError('Windows не выполнила действие или подтверждение отменено.')

def sound_settings():
    shell_open('ms-settings:sound')

def install_driver():
    powershell=Path(os.environ.get('SystemRoot','C:\\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    shell_open(powershell, subprocess.list2cmdline(['-NoProfile','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File',str(ROOT/'installer/Install-VBCable.ps1')]))

def rename_endpoint(ident):
    if not isinstance(ident,str) or not 1 <= len(ident) <= 512:
        raise BridgeError('Неверный идентификатор выхода Windows.')
    executable=Path(sys.executable).with_name('pythonw.exe')
    if not executable.is_file():
        executable=Path(sys.executable)
    shell_open(executable, subprocess.list2cmdline([str(ROOT/'advanced_entry.py'),'--rename-endpoint',ident]), 'runas')

def message(text, error=False):
    if os.name == 'nt':
        ctypes.windll.user32.MessageBoxW(None,str(text),'Yandex Station Advanced',0x10 if error else 0x40)


def firewall():
    powershell=Path(os.environ.get('SystemRoot','C:\\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    shell_open(powershell,subprocess.list2cmdline(['-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'installer/Firewall.ps1')]),'runas')
