"""Run the unmodified 1.0.6 engine using an installed private Python runtime.

Only the new Recovery data directory is written. Existing applications, drivers,
accounts and their runtime files are never overwritten. Audio capture is manual.
"""
from __future__ import annotations
import asyncio
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import traceback

ROOT = Path(__file__).resolve().parent
DATA_NAME = 'StationBridgePCM106Recovery'
TITLE = 'Station Bridge — исходный PCM 1.0.6'


def message(text: str, flags: int = 0x40) -> int:
    if os.name != 'nt':
        print(text)
        return 0
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    user.MessageBoxW.restype = ctypes.c_int
    return user.MessageBoxW(None, text, TITLE, flags)


def validate_original_files(root: Path = ROOT) -> None:
    data = json.loads((root / 'ORIGINAL_ENGINE_MANIFEST.json').read_text('utf-8'))
    for row in data['unchanged']:
        path = (root / row['path']).resolve()
        if not path.is_relative_to(root.resolve()):
            raise RuntimeError('Недопустимый путь в списке файлов.')
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise RuntimeError('Изменён или отсутствует файл исходной 1.0.6: ' + row['path'] +
                               '\nРаспакуйте архив в новую папку, не поверх Advanced.')


def filtered_settings(raw: dict) -> dict:
    # No spatial processing, group, timer, endpoint or experiment settings.
    permitted = {'pins', 'audio_host_overrides', 'audio_peer_approvals', 'device'}
    result = {key: raw[key] for key in permitted if isinstance(raw.get(key), dict)}
    result['remember'] = raw.get('remember') is True
    return result


def copy_settings(source: Path, target: Path) -> bool:
    """Copy on explicit consent only, once. The source is read-only."""
    if source.resolve() == target.resolve():
        raise ValueError('Source and target must differ')
    if (target / 'settings.json').exists():
        return False
    raw = json.loads((source / 'settings.json').read_text('utf-8'))
    if not isinstance(raw, dict):
        raise ValueError('Некорректный файл настроек')
    data = filtered_settings(raw)
    target.mkdir(parents=True, exist_ok=True)
    if data['remember'] and (source / 'account.dpapi').is_file():
        if (target / 'account.dpapi').exists():
            raise ValueError('В резервной папке уже существует вход. Он не был заменён.')
        # Still encrypted. No decryption or logging during the copy.
        shutil.copyfile(source / 'account.dpapi', target / 'account.dpapi')
    else:
        data['remember'] = False
    with (target / 'settings.json').open('x', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return True


def offer_import(local: Path, target: Path) -> None:
    if (target / 'settings.json').exists() or (target / 'first_start.done').exists():
        return
    source = next((local / name for name in ('YandexStationAdvanced', 'YandexStationBridge')
                   if (local / name / 'settings.json').is_file()), None)
    if source and message('Скопировать сохранённый вход, адреса колонок и разрешения из «' +
            source.name + '» в отдельную резервную папку?\n\n'
            'Оригиналы не изменятся. Группа, обработка каналов и эксперименты не переносятся.\n'
            'Можно выбрать «Нет» и войти в резервную версию по QR.', 0x24 | 0x100) == 6:
        copy_settings(source, target)
    (target / 'first_start.done').write_text('Recovery setup completed; no audio was started.\n', 'utf-8')


def isolate_application_path() -> None:
    # Embedded Python ._pth often includes the Advanced install directory.
    # Retain its runtime/stdlib/dependencies but never import its app modules.
    runtime = Path(sys.executable).resolve().parent
    kept = []
    for p in sys.path:
        if p and Path(p).resolve().is_relative_to(runtime):
            kept.append(p)
    if os.name == 'nt':
        sys.path[:] = [str(ROOT), *kept]
    else:
        sys.path.insert(0, str(ROOT))
    sys.dont_write_bytecode = True


class PrivateLog:
    def __init__(self, file):
        self.file = file
    def write(self, text):
        cleaned = re.sub(r'(#key=)[^\s]+', r'\1[REDACTED]', text)
        self.file.write(cleaned)
        self.file.flush()
        return len(text)
    def flush(self):
        self.file.flush()


async def run_recovery(directory: Path) -> None:
    """Only the control-panel binding differs from the original app.run()."""
    import secrets
    import socket
    import webbrowser
    from aiohttp import web
    from station_bridge.app import Controller, make_ui_app
    from station_bridge.protocol import BridgeError

    # Preflight only; never kill/stop another application holding the audio port.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == 'nt':
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(('0.0.0.0', 8808))
    except OSError as exc:
        raise BridgeError('Аудиопорт 8808 занят. Завершите Advanced и прежнюю Station Bridge '
                          'через кнопку «Завершить», затем повторите запуск.') from exc
    finally:
        probe.close()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    controller = runner = None
    try:
        if os.name == 'nt':
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(('127.0.0.1', 8789))
        except OSError:
            listener.bind(('127.0.0.1', 0))
        listener.listen(16)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        controller = Controller(directory)
        await controller.start()
        secret = secrets.token_urlsafe(32)
        runner = web.AppRunner(make_ui_app(controller, secret, port), access_log=None, shutdown_timeout=3)
        await runner.setup()
        await web.SockSite(runner, listener).start()
        url = f'http://127.0.0.1:{port}/#key={secret}'
        print('Recovery: исходный аудиодвижок 1.0.6. Захват ещё не включён.', flush=True)
        opened = await asyncio.to_thread(webbrowser.open, url)
        if not opened:
            message('Откройте в браузере эту личную ссылку:\n' + url +
                    '\n\nНе пересылайте её другим. Остановка — кнопка «Завершить» в панели.')
        await controller.shutdown.wait()
    finally:
        if controller:
            await controller.close()
        if runner:
            await runner.cleanup()
        listener.close()


def acquire_mutex():
    if os.name != 'nt':
        return None, None
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.CloseHandle.restype = ctypes.c_int
    handle = k.CreateMutexW(None, False, 'Local\\StationBridgePCM106Recovery')
    err = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(err)
    if err == 183:
        k.CloseHandle(handle)
        raise RuntimeError('Резервная 1.0.6 уже открыта. Используйте её вкладку браузера; '
                           'остановка — кнопка «Завершить».')
    return k, handle


def main() -> int:
    mutex = kernel = log = None
    old_out, old_err = sys.stdout, sys.stderr
    data = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / DATA_NAME
    try:
        kernel, mutex = acquire_mutex()
        validate_original_files()
        isolate_application_path()
        data.mkdir(parents=True, exist_ok=True)
        offer_import(data.parent, data)
        log_path = data / 'recovery.log'
        if log_path.exists() and log_path.stat().st_size > 1_000_000:
            log_path.replace(data / 'recovery.previous.log')
        log = log_path.open('a', encoding='utf-8', buffering=1)
        sys.stdout = sys.stderr = PrivateLog(log)
        import station_bridge
        if not Path(station_bridge.__file__).resolve().is_relative_to(ROOT / 'station_bridge'):
            raise RuntimeError('Неверная папка аудиодвижка. Запуск остановлен.')
        asyncio.run(run_recovery(data))
        return 0
    except Exception as exc:
        if log:
            traceback.print_exc()
        message(str(exc) + '\n\nЖурнал резервного запуска: ' + str(data / 'recovery.log'), 0x10)
        return 1
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        if log:
            log.close()
        if kernel and mutex:
            kernel.CloseHandle(mutex)


if __name__ == '__main__':
    raise SystemExit(main())
