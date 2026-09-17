"""Station Bridge for Windows. Launch with Start.cmd, or python main.py."""
import argparse
import asyncio
import os
from pathlib import Path
import sys
from station_bridge.app import run
from station_bridge.protocol import BridgeError


def main():
    parser = argparse.ArgumentParser(description='Windows -> Yandex Station over LAN')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=8769)
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'YandexStationBridge')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be 1024..65535')
    if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    try:
        asyncio.run(run(args.data_dir, not args.no_browser, args.port))
    except KeyboardInterrupt:
        print('\nStation Bridge остановлен.')
    except BridgeError as exc:
        print('\nОшибка: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
