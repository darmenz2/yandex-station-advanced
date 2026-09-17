"""Finite WAV presentation of live PCM. No RF64 or unknown-length chunks.

A reconnect starts a NEW live presentation with a fresh header, not a seek
within recorded audio. Range is ignored with HTTP 200 by the media handler.
Stopping aborts an unfinished transfer. This path is experimental on a Station.
"""
from __future__ import annotations
import struct

MAX_RIFF_DATA = 0xffffffff - 36


def wav_header(rate: int, channels: int, data_bytes: int) -> bytes:
    """44-byte canonical PCM/S16LE header; lengths match the HTTP frame budget."""
    if channels not in (1, 2) or not 8000 <= rate <= 192000:
        raise ValueError('PCM supports mono/stereo at 8–192 kHz')
    align = channels * 2
    if not 0 <= data_bytes <= MAX_RIFF_DATA or data_bytes % align:
        raise ValueError('Unaligned or excessive WAV data length')
    return struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 36 + data_bytes,
                       b'WAVE', b'fmt ', 16, 1, channels, rate,
                       rate * align, align, 16, b'data', data_bytes)


def pcm_response_blocks(rate: int, channels: int, frames: int) -> int:
    if frames < 1:
        raise ValueError('Invalid PCM block length')
    wav_header(rate, channels, 0)
    # Up to 6 h, shortened if necessary to stay within classic RIFF's 32-bit size.
    return min(int(rate * 21600 // frames), MAX_RIFF_DATA // (frames * channels * 2))
