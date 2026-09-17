"""Keep import/startup errors visible even when Python has no console."""
from __future__ import annotations
import ctypes
import os
from pathlib import Path
import sys
import traceback

if __name__ == '__main__':
    try:
        from advanced_entry import main
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        folder = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'YandexStationAdvanced'
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / 'desktop.log').open('a', encoding='utf-8') as stream:
                traceback.print_exc(file=stream)
        except OSError:
            pass
        if os.name == 'nt':
            ctypes.windll.user32.MessageBoxW(None,
                'Ошибка запуска: ' + type(exc).__name__ + '.\nПодробности: ' + str(folder / 'desktop.log') +
                '\nУстановка компонентов и запуск приложения — разные этапы. Повторно скачивать всё не требуется.',
                'Yandex Station Advanced', 0x10)
        raise SystemExit(1)
