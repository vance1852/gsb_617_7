from __future__ import annotations

import glob
import hashlib
import os
from pathlib import Path
from typing import Iterable


def _hash_file(path: Path, block_size: int = 65536) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(block_size)
                if not chunk:
                    break
                h.update(chunk)
    except (OSError, PermissionError):
        return ""
    return h.hexdigest()


def _expand_patterns(patterns: Iterable[str], base_dir: Path | None = None) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        if base_dir:
            full_pattern = str(base_dir / pattern) if not os.path.isabs(pattern) else pattern
        else:
            full_pattern = pattern
        matched = glob.glob(full_pattern, recursive=True)
        for m in matched:
            p = Path(m)
            if p.is_file():
                files.append(p.resolve())
    return sorted(set(files))


def compute_fingerprint(
    patterns: Iterable[str],
    base_dir: Path | None = None,
) -> dict[str, tuple[str, float]]:
    """Return dict of filepath -> (sha256, mtime) for matching files."""
    result: dict[str, tuple[str, float]] = {}
    for f in _expand_patterns(patterns, base_dir):
        key = str(f)
        try:
            mtime = f.stat().st_mtime
            digest = _hash_file(f)
            result[key] = (digest, mtime)
        except OSError:
            pass
    return result


def outputs_exist(patterns: Iterable[str], base_dir: Path | None = None) -> bool:
    files = _expand_patterns(patterns, base_dir)
    return len(files) > 0


def is_up_to_date(
    inputs: Iterable[str],
    outputs: Iterable[str],
    old_inputs_fingerprint: dict[str, tuple[str, float]],
    base_dir: Path | None = None,
) -> bool:
    if not outputs:
        return False
    if not outputs_exist(outputs, base_dir):
        return False
    current_fingerprint = compute_fingerprint(inputs, base_dir)
    return current_fingerprint == old_inputs_fingerprint
