"""Reserve the settings socket before constructing its Host/Origin boundary.

Only loopback is bound. An occupied port is never taken from another process;
no request is sent to the unknown service and no existing process is stopped.
"""
from __future__ import annotations

import errno
import socket


def reserve_panel_socket(preferred: int = 8779, attempts: int = 10) -> socket.socket:
    if isinstance(preferred, bool) or not isinstance(preferred, int) or not 1024 <= preferred <= 65535:
        raise ValueError('preferred must be an integer from 1024 to 65535')
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 16:
        raise ValueError('attempts must be an integer from 1 to 16')
    candidates = list(range(preferred, min(preferred + attempts, 65536))) + [0]
    for port in candidates:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(('127.0.0.1', port))
            sock.listen(128)
            sock.setblocking(False)
            return sock
        except OSError as exc:
            sock.close()
            retryable = (exc.errno in (errno.EADDRINUSE, errno.EACCES) or
                         getattr(exc, 'winerror', None) in (10048, 10013))
            if port == 0 or not retryable:
                raise
    raise RuntimeError('No loopback port could be reserved')
