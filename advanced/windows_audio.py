"""Core Audio endpoint control via documented COM interfaces, no third-party DLL.

One worker serialises COM calls and initialises/uninitialises COM on that thread.
Master gain belongs to the Windows endpoint. It is deliberately NOT multiplied
again by our PCM path or copied to each speaker's hardware volume.
"""
from __future__ import annotations
import asyncio
import ctypes as C
from concurrent.futures import ThreadPoolExecutor
import math
import os
import uuid
from station_bridge.protocol import BridgeError

HRESULT = C.c_int32
DWORD = C.c_uint32
BOOL = C.c_int32
LPVOID = C.c_void_p

class GUID(C.Structure):
    _fields_ = [('bytes', C.c_ubyte * 16)]
    @classmethod
    def make(cls, value):
        return cls((C.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))

class PROPERTYKEY(C.Structure):
    _fields_ = [('fmtid', GUID), ('pid', DWORD)]

class PVUnion(C.Union):
    _fields_ = [('pointer', LPVOID), ('padding', C.c_ubyte * 16)]

class PROPVARIANT(C.Structure):
    _fields_ = [('vt', C.c_uint16), ('reserved', C.c_uint16 * 3), ('value', PVUnion)]

FRIENDLY = PROPERTYKEY(GUID.make('a45c254e-df1c-4efd-8020-67d146a850e0'), 14)
ENUM_CLSID = GUID.make('bcde0395-e52f-467c-8e3d-c4579291692e')
ENUM_IID = GUID.make('a95664d2-9614-4f35-a746-de8db63617e6')
VOLUME_IID = GUID.make('5cdf2c82-841e-4546-9722-0cf74078229a')
NEW_NAME = 'Яндекс станция'


def virtual_name(name):
    text = str(name).casefold()
    return text in (NEW_NAME.casefold(),'yandex station advanced') or ('cable input' in text and 'vb-audio' in text)


def choose_loopback(name, outputs):
    def normal(value):
        return str(value).removesuffix(' [Loopback]').strip().casefold()
    matches = [d for d in outputs if normal(d.get('name', '')) == normal(name)]
    if not matches:
        target=normal(name)
        matches=[d for d in outputs if normal(d.get('name','')).startswith(target+' (')]
    if len(matches) != 1:
        raise BridgeError('Не найден однозначный WASAPI loopback для «' + str(name) +
                          '». Обновите устройства; при одинаковых именах переименуйте выходы в Windows.')
    return matches[0]


def validate_volume(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise BridgeError('Общая громкость должна быть числом от 0 до 100%.')
    return float(value)


def check(result):
    if result < 0:
        raise BridgeError(f'Core Audio вернул HRESULT 0x{result & 0xffffffff:08X}. Обновите звуковые устройства.')


def call(obj, index, restype, argtypes, *args):
    address = C.cast(obj, C.POINTER(C.POINTER(LPVOID))).contents[index]
    return C.WINFUNCTYPE(restype, LPVOID, *argtypes)(address)(obj, *args)


def release(obj):
    if obj:
        call(obj, 2, DWORD, [])


class CoreAudio:
    def __enter__(self):
        if os.name != 'nt':
            raise BridgeError('Core Audio доступен только в Windows.')
        self.ole = C.WinDLL('ole32')
        self.ole.CoInitializeEx.argtypes = [LPVOID, DWORD]
        self.ole.CoInitializeEx.restype = HRESULT
        self.ole.CoCreateInstance.argtypes = [C.POINTER(GUID), LPVOID, DWORD, C.POINTER(GUID), C.POINTER(LPVOID)]
        self.ole.CoCreateInstance.restype = HRESULT
        self.ole.CoTaskMemFree.argtypes = [LPVOID]
        self.ole.PropVariantClear.argtypes = [C.POINTER(PROPVARIANT)]
        self.ole.PropVariantClear.restype = HRESULT
        check(self.ole.CoInitializeEx(None, 0))
        self.enumerator = LPVOID()
        try:
            check(self.ole.CoCreateInstance(C.byref(ENUM_CLSID), None, 1, C.byref(ENUM_IID), C.byref(self.enumerator)))
        except BaseException:
            self.ole.CoUninitialize()
            raise
        return self

    def __exit__(self, *exc):
        release(self.enumerator)
        self.ole.CoUninitialize()

    def device(self, ident):
        obj = LPVOID()
        check(call(self.enumerator, 5, HRESULT, [C.c_wchar_p, C.POINTER(LPVOID)], str(ident), C.byref(obj)))
        return obj

    def friendly(self, device):
        props = LPVOID()
        check(call(device, 4, HRESULT, [DWORD, C.POINTER(LPVOID)], 0, C.byref(props)))
        value = PROPVARIANT()
        try:
            check(call(props, 5, HRESULT, [C.POINTER(PROPERTYKEY), C.POINTER(PROPVARIANT)], C.byref(FRIENDLY), C.byref(value)))
            return C.wstring_at(value.value.pointer) if value.vt == 31 and value.value.pointer else 'Аудиовыход'
        finally:
            self.ole.PropVariantClear(C.byref(value))
            release(props)

    def endpoints(self):
        collection, count = LPVOID(), DWORD()
        check(call(self.enumerator, 3, HRESULT, [C.c_int32, DWORD, C.POINTER(LPVOID)], 0, 1, C.byref(collection)))
        answer = []
        try:
            check(call(collection, 3, HRESULT, [C.POINTER(DWORD)], C.byref(count)))
            for i in range(count.value):
                device, ident = LPVOID(), LPVOID()
                check(call(collection, 4, HRESULT, [DWORD, C.POINTER(LPVOID)], i, C.byref(device)))
                try:
                    check(call(device, 5, HRESULT, [C.POINTER(LPVOID)], C.byref(ident)))
                    name = self.friendly(device)
                    fmt={}
                    try:fmt=self.mix_format(device)
                    except BridgeError:pass
                    answer.append({'id': C.wstring_at(ident), 'name': name, 'virtual_cable': virtual_name(name), 'mix_format':fmt})
                finally:
                    if ident:
                        self.ole.CoTaskMemFree(ident)
                    release(device)
        finally:
            release(collection)
        return answer

    def mix_format(self,device):
        import struct
        iid=GUID.make('1cb9ad4c-dbfa-4c32-b178-c2f568a703b2')
        client=LPVOID();fmt=LPVOID()
        try:
            check(call(device,3,HRESULT,[C.POINTER(GUID),DWORD,LPVOID,C.POINTER(LPVOID)],C.byref(iid),23,None,C.byref(client)))
            check(call(client,8,HRESULT,[C.POINTER(LPVOID)],C.byref(fmt)))
            if not fmt:raise BridgeError('Windows не вернула формат выхода.')
            tag,channels,rate,avg,align,bits,extra=struct.unpack('<HHIIHHH',C.string_at(fmt,18))
            mask=struct.unpack_from('<I',C.string_at(fmt,40),20)[0] if tag==0xfffe and extra>=22 else 3 if channels==2 else 4 if channels==1 else 0
            return {'rate':rate,'channels':channels,'bits':bits,'channel_mask':mask}
        finally:
            if fmt:self.ole.CoTaskMemFree(fmt)
            release(client)

    def volume(self, ident, value=None, mute=None):
        device = self.device(ident)
        endpoint = LPVOID()
        try:
            check(call(device, 3, HRESULT, [C.POINTER(GUID), DWORD, LPVOID, C.POINTER(LPVOID)],
                       C.byref(VOLUME_IID), 23, None, C.byref(endpoint)))
            if value is not None:
                check(call(endpoint, 7, HRESULT, [C.c_float, LPVOID], validate_volume(value), None))
            if mute is not None:
                check(call(endpoint, 14, HRESULT, [BOOL, LPVOID], bool(mute), None))
            v, m = C.c_float(), BOOL()
            check(call(endpoint, 9, HRESULT, [C.POINTER(C.c_float)], C.byref(v)))
            check(call(endpoint, 15, HRESULT, [C.POINTER(BOOL)], C.byref(m)))
            return {'volume': round(v.value, 4), 'mute': bool(m.value)}
        finally:
            release(endpoint)
            release(device)

    def rename(self, ident):
        # Called explicitly by an elevated helper, never during ordinary startup.
        device = self.device(ident)
        props = LPVOID()
        try:
            old_name = self.friendly(device)
            if not virtual_name(old_name):
                raise BridgeError('Переименовывать можно только VB-CABLE или ранее настроенный выход приложения.')
            check(call(device, 4, HRESULT, [DWORD, C.POINTER(LPVOID)], 2, C.byref(props)))
            text = C.create_unicode_buffer(NEW_NAME)
            value = PROPVARIANT()
            value.vt = 31
            value.value.pointer = C.cast(text, LPVOID).value
            check(call(props, 6, HRESULT, [C.POINTER(PROPERTYKEY), C.POINTER(PROPVARIANT)], C.byref(FRIENDLY), C.byref(value)))
            check(call(props, 7, HRESULT, []))
            # The buffer is Python-owned, not PropVariantClear-owned.
            return NEW_NAME
        finally:
            release(props)
            release(device)


def get_status(ident):
    if os.name != 'nt':
        return {'available': False, 'endpoints': [], 'selected': None, 'error': 'Core Audio: требуется Windows.'}
    with CoreAudio() as audio:
        outputs = audio.endpoints()
        selected = next((d.copy() for d in outputs if d['id'] == ident), None)
        if selected:
            selected.update(audio.volume(ident))
        return {'available': True, 'endpoints': outputs, 'selected': selected, 'error': ''}


def set_volume(ident, volume, mute):
    if volume is not None:
        validate_volume(volume)
    with CoreAudio() as audio:
        return audio.volume(ident, volume, mute)


class AudioWorker:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='CoreAudio')
    async def status(self, ident=''):
        return await asyncio.get_running_loop().run_in_executor(self.pool, get_status, ident)
    async def set(self, ident, volume=None, mute=None):
        return await asyncio.get_running_loop().run_in_executor(self.pool, set_volume, ident, volume, mute)
    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
