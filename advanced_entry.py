"""Tray-owned desktop application. Does not start recording at logon."""
from __future__ import annotations
import argparse
import asyncio
import contextlib
import ctypes
import os
from pathlib import Path
import secrets
import sys
import webbrowser
from aiohttp import web
from advanced.reliable import ReliableController as DesktopController
from advanced import __version__
from advanced.listener import reserve_panel_socket
from station_bridge.app import make_ui_app
from station_bridge.protocol import BridgeError

ROOT=Path(__file__).resolve().parent

async def desktop(directory, open_browser=True, no_tray=False, port=8779):
    controller=DesktopController(directory)
    secret=secrets.token_urlsafe(32)
    url = ''
    requested_port = port
    listener = None
    runner=tray=updater=None
    tasks=set()
    async def command(name, data):
        try:
            if name == 'open':
                tray.show() if tray else None
            elif name == 'advanced':
                result=await controller.advanced_action(data.get('command',''),data)
                if tray and result.get('path'):tray.toast('Отчёт: '+result['path'])
            elif name == 'start':
                await controller.action('live', controller.last_launch_options)
            elif name in ('tray_failed','tray_error'):
                controller.log('Ошибка значка Windows: ' + data.get('message',''))
                if name == 'tray_failed':
                    controller.shutdown.set()
            elif name in ('master_volume','master_mute','station_volume','sound_settings'):
                await controller.advanced_action(name,data)
            else:
                await controller.action(name,data)
        except BridgeError as exc:
            controller.log(str(exc))
            if tray: tray.toast(str(exc))
        except Exception as exc:
            controller.log('Команда не выполнена: '+type(exc).__name__)
            if tray: tray.toast('Команда: '+type(exc).__name__)
    loop=asyncio.get_running_loop()
    def dispatch(name,data):
        def schedule():
            task=asyncio.create_task(command(name,data)); tasks.add(task)
            task.add_done_callback(tasks.discard)
        loop.call_soon_threadsafe(schedule)
    async def update_tray():
        while True:
            peers=[{'id':p.device['id'],'name':p.device['name'],'volume':p.glagol.safe_state().get('volume',0)} for p in controller.all_peers()]
            tray.update(peers,controller.endpoint_state.get('selected'),controller.media.kind=='live',controller.native_snapshot())
            await asyncio.sleep(.25)
    try:
        listener = reserve_panel_socket(port)
        port = listener.getsockname()[1]
        url = f'http://127.0.0.1:{port}/#key={secret}'
        await controller.start()
        if port != requested_port:
            controller.log(f'Порт панели {requested_port} занят; панель открыта на свободном порту {port}.')
        app=make_ui_app(controller,secret,port)
        runner=web.AppRunner(app,access_log=None,shutdown_timeout=3)
        await runner.setup()
        try:
            await web.SockSite(runner, listener).start()
        except OSError as exc:
            raise BridgeError('Не удалось открыть локальную панель. Подробности в desktop.log.') from exc
        if not no_tray:
            from advanced.tray import Tray
            tray=Tray(ROOT/'assets/app.ico',dispatch)
            await asyncio.to_thread(tray.start)
            updater=asyncio.create_task(update_tray())
        print('Yandex Station Advanced '+__version__+' — запущен. Остановка через меню значка.',flush=True)
        if no_tray and sys.stdout:
            print('Панель (приватная ссылка): '+url,flush=True)
        if open_browser:
            tray.show() if tray else None
        await controller.shutdown.wait()
    finally:
        if updater:
            updater.cancel()
            with contextlib.suppress(asyncio.CancelledError): await updater
        if tray:
            await asyncio.to_thread(tray.stop)
        for task in list(tasks): task.cancel()
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)
        # Close consumers/producer before shutting down HTTP listeners.
        await controller.close()
        if runner: await runner.cleanup()
        if listener: listener.close()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--tray',action='store_true',help='Do not open settings automatically')
    parser.add_argument('--no-tray',action='store_true',help='Development: console owns app lifetime')
    parser.add_argument('--data-dir',type=Path,default=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'YandexStationAdvanced')
    parser.add_argument('--port',type=int,default=8779)
    parser.add_argument('--rename-endpoint')
    args=parser.parse_args()
    if not 1024 <= args.port <= 65535: parser.error('port must be 1024..65535')
    from advanced.native_helper import message
    if args.rename_endpoint:
        try:
            from advanced.windows_audio import CoreAudio
            with CoreAudio() as audio: audio.rename(args.rename_endpoint)
            message('Выход назван «Яндекс станция». Обновите устройства в меню и выберите его в стандартных настройках Windows.')
            return 0
        except Exception as exc:
            message(str(exc) if isinstance(exc,BridgeError) else type(exc).__name__,True)
            return 1
    mutex=None
    if os.name=='nt':
        with contextlib.suppress(AttributeError,OSError):
            u=ctypes.WinDLL('user32');u.SetProcessDpiAwarenessContext.argtypes=[ctypes.c_void_p]
            u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p]
        kernel.CreateMutexW.restype=ctypes.c_void_p
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        # Session-local prevents normal per-desktop duplicates without cross-user tokens.
        mutex=kernel.CreateMutexW(None,False,'Local\\YandexStationAdvanced.Desktop')
        if not mutex: raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error()==183:
            message('Приложение уже работает. Нажмите его значок рядом с часами (возможно, под стрелкой ↑).')
            kernel.CloseHandle(mutex)
            return 0
    logfile=None
    try:
        args.data_dir.mkdir(parents=True,exist_ok=True)
        if sys.stdout is None:
            log=args.data_dir/'desktop.log'
            if log.exists() and log.stat().st_size>2_000_000:
                log.replace(args.data_dir/'desktop.previous.log')
            logfile=log.open('a',encoding='utf-8',buffering=1)
            sys.stdout=sys.stderr=logfile
        asyncio.run(desktop(args.data_dir,not args.tray,args.no_tray,args.port))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        text=str(exc) if isinstance(exc,BridgeError) else type(exc).__name__+': ошибка запуска. См. desktop.log.'
        print(text,flush=True)
        message(text,True)
        return 1
    finally:
        if mutex: kernel.CloseHandle(mutex)
        if logfile: logfile.close()
    return 0

if __name__=='__main__':
    raise SystemExit(main())
