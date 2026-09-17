"""Windows DPAPI credentials, network validation, certificate pinning."""
from __future__ import annotations
import asyncio
import ctypes
import hashlib
import ipaddress
import json
import os
import socket
import ssl
from pathlib import Path
from .protocol import BridgeError


def validate_lan_ip(value: str, allow_loopback: bool = False) -> str:
    try:
        ip = ipaddress.IPv4Address(value.strip())
    except ipaddress.AddressValueError as exc:
        raise BridgeError('Нужен IPv4-адрес колонки, например 192.168.1.50.') from exc
    ranges = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16')
    if not any(ip in ipaddress.IPv4Network(r) for r in ranges):
        if not (allow_loopback and ip.is_loopback):
            raise BridgeError('Поддерживаются только локальные IPv4-адреса. Публичный IP не принимается.')
    return str(ip)


def local_ip_for(station_ip: str) -> str:
    # UDP connect determines a route; it sends no packet.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((station_ip, 1961))
        return sock.getsockname()[0]


async def get_fingerprint(host: str, port: int) -> str:
    # This unauthenticated connection retrieves ONLY the public certificate.
    # Credentials are sent later over the fingerprint-verified WebSocket.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx), 10)
        try:
            cert = writer.get_extra_info('ssl_object').getpeercert(binary_form=True)
            if not cert:
                raise BridgeError('Колонка не предоставила TLS-сертификат.')
            return hashlib.sha256(cert).hexdigest()
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (TimeoutError, OSError):
                pass
    except (OSError, asyncio.TimeoutError) as exc:
        raise BridgeError(f'Нет соединения с {host}:{port}. Проверьте адрес и общую локальную сеть.') from exc


def _crypt(raw: bytes, decrypt: bool = False) -> bytes:
    if os.name != 'nt':
        raise BridgeError('Сохранение токена поддерживается только в Windows через DPAPI.')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt32.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        fn = crypt32.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), 'Station Bridge', None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise BridgeError('Windows DPAPI не смог обработать токен. Войдите заново без сохранения.')
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


class Settings:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.config_path = directory / 'settings.json'
        self.secret_path = directory / 'account.dpapi'
        try:
            self.data = json.loads(self.config_path.read_text('utf-8'))
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        tmp = self.config_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), 'utf-8')
        tmp.replace(self.config_path)

    def save_account(self, account: dict) -> None:
        protected = _crypt(json.dumps(account).encode())
        tmp = self.secret_path.with_suffix('.tmp')
        tmp.write_bytes(protected)
        tmp.replace(self.secret_path)

    def load_account(self) -> dict:
        if not self.secret_path.exists():
            return {}
        return json.loads(_crypt(self.secret_path.read_bytes(), True))

    def forget_account(self) -> None:
        self.secret_path.unlink(missing_ok=True)
