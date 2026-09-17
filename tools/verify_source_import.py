#!/usr/bin/env python3
"""Check the imported snapshot and the original root LICENSE without dependencies."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import sys

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    try:
        meta = json.loads((ROOT / "SOURCE_IMPORT.json").read_text(encoding="utf-8"))
        if meta.get("format") != 1:
            raise ValueError("Unsupported manifest format")
        failures: list[str] = []
        seen: set[str] = set()
        for row in meta["files"]:
            name = row["path"]
            path = PurePosixPath(name)
            if (not name or path.is_absolute() or ".." in path.parts
                    or "\\" in name or ":" in name or path.parts[0] == ".git"):
                raise ValueError(f"Unsafe path in manifest: {name!r}")
            if name in seen:
                raise ValueError(f"Duplicate manifest path: {name}")
            seen.add(name)
            target = ROOT.joinpath(*path.parts)
            if target.is_symlink() or not target.resolve().is_relative_to(ROOT):
                failures.append(f"Unsafe file path: {name}")
                continue
            if not target.is_file():
                failures.append(f"Missing: {name}")
                continue
            raw = target.read_bytes()
            if len(raw) != row["size"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
                failures.append(f"Changed: {name}")
        raw = (ROOT / "LICENSE").read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        if blob != meta["preserved_license_git_blob"]:
            failures.append("Root LICENSE differs from the original GitHub blob")
        if failures:
            print("\n".join(failures), file=sys.stderr)
            return 1
        print(f"OK: {len(seen)} imported files verified; original root LICENSE preserved.")
        print("This is an integrity check, not a hardware or audio-quality test.")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Import verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
