"""Reversible, best-effort Windows scheduling for the fast writer only.

No registry edits, process-wide REALTIME priority or admin elevation. APIs may
fail (e.g. disabled MMCSS); audio still runs and exposes the result in diagnostics.
The timer request may increase power use during the active fast stream.
"""
from __future__ import annotations
from contextlib import contextmanager, suppress
import ctypes
import sys


def _apis():
    winmm = ctypes.WinDLL('winmm', use_last_error=True)
    winmm.timeBeginPeriod.argtypes = [ctypes.c_uint]
    winmm.timeBeginPeriod.restype = ctypes.c_uint
    winmm.timeEndPeriod.argtypes = [ctypes.c_uint]
    winmm.timeEndPeriod.restype = ctypes.c_uint
    avrt = ctypes.WinDLL('avrt', use_last_error=True)
    avrt.AvSetMmThreadCharacteristicsW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    avrt.AvSetMmThreadCharacteristicsW.restype = ctypes.c_void_p
    avrt.AvRevertMmThreadCharacteristics.argtypes = [ctypes.c_void_p]
    avrt.AvRevertMmThreadCharacteristics.restype = ctypes.c_int
    return winmm, avrt


@contextmanager
def audio_thread_scope(enabled: bool):
    state = {'timer_1ms': False, 'mmcss_audio': False}
    timer_started = False
    winmm = avrt = None
    handle = None
    try:
        if enabled and sys.platform == 'win32':
            with suppress(OSError, AttributeError):
                winmm, avrt = _apis()
                timer_started = winmm.timeBeginPeriod(1) == 0
                state['timer_1ms'] = timer_started
                index = ctypes.c_uint32(0)
                handle = avrt.AvSetMmThreadCharacteristicsW('Audio', ctypes.byref(index))
                state['mmcss_audio'] = bool(handle)
        yield state
    finally:
        # Both calls are matched on the thread that acquired the resource.
        if handle and avrt:
            with suppress(OSError):
                avrt.AvRevertMmThreadCharacteristics(handle)
        if timer_started and winmm:
            with suppress(OSError):
                winmm.timeEndPeriod(1)
