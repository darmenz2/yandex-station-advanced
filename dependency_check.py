"""Check the private runtime without passing Python code through PowerShell.

Only the pinned requirement syntax used by this project is accepted. A failed
check never records an installation marker and never starts audio capture.
"""
from __future__ import annotations

import argparse
import importlib
from importlib import metadata
from pathlib import Path
import re
import subprocess
import sys


MODULES = ("aiohttp", "zeroconf", "qrcode", "imageio_ffmpeg")


def check_requirements(path: Path) -> list[str]:
    errors: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        spec, separator, marker = line.partition(";")
        if separator:
            match = re.fullmatch(r"sys_platform\s*==\s*(['\"])([a-z0-9_]+)\1", marker.strip())
            if not match:
                raise ValueError(f"Unsupported requirement marker on line {number}")
            if match[2] != sys.platform:
                continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([A-Za-z0-9.+_-]+)", spec.strip())
        if not match:
            raise ValueError(f"Expected a pinned requirement on line {number}")
        name, expected = match.groups()
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            errors.append(f"{name}: not installed (required {expected})")
        else:
            if installed != expected:
                errors.append(f"{name}: installed {installed}, required {expected}")
    return errors


def check_components() -> str:
    for module in MODULES:
        importlib.import_module(module)
    if sys.platform == "win32":
        # Import the extension, but do not open a device or capture any audio.
        importlib.import_module("pyaudiowpatch")
    ffmpeg = Path(importlib.import_module("imageio_ffmpeg").get_ffmpeg_exe())
    if not ffmpeg.is_file():
        raise RuntimeError(f"FFmpeg executable is missing: {ffmpeg}")
    result = subprocess.run(
        [str(ffmpeg), "-version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=15,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if result.returncode:
        raise RuntimeError(f"FFmpeg self-check exited with code {result.returncode}")
    return str(ffmpeg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=Path(__file__).with_name("requirements.txt"))
    args = parser.parse_args(argv)
    # Embedded Python may otherwise use a legacy Windows console encoding.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        errors = check_requirements(args.requirements)
        if errors:
            for error in errors:
                print("Dependency check: " + error)
            return 1
        executable = check_components()
    except Exception as exc:
        print(f"Dependency check: {type(exc).__name__}: {exc}")
        return 1
    print("FFmpeg:", executable)
    print("Dependency self-check OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
