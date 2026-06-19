from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Dict, List, Tuple


def _expand_globs(patterns: List[str], base_dir: str | None = None) -> List[Path]:
    results: List[Path] = []
    seen: set[Path] = set()
    base = Path(base_dir) if base_dir else Path.cwd()
    for pat in patterns:
        p = Path(pat)
        if not p.is_absolute():
            p = base / p
        if any(ch in pat for ch in "*?["):
            parent = p.parent
            glob_pat = p.name
            root = parent if parent.exists() else base
            matches = sorted(root.rglob(glob_pat)) if "**" in pat or parent.name == "**" else sorted(root.glob(glob_pat))
            for m in matches:
                rp = m.resolve()
                if rp.is_file() and rp not in seen:
                    seen.add(rp)
                    results.append(rp)
        else:
            rp = p.resolve()
            if rp.exists() and rp.is_file() and rp not in seen:
                seen.add(rp)
                results.append(rp)
    return results


def _hash_file(path: Path, chunk_size: int = 65536) -> str:
    h = hashlib.sha256()
    try:
        st = path.stat()
        h.update(str(st.st_size).encode())
        h.update(str(st.st_mtime_ns).encode())
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def fingerprint_files(patterns: List[str], base_dir: str | None = None) -> Dict[str, Tuple[str, float]]:
    fps: Dict[str, Tuple[str, float]] = {}
    for f in _expand_globs(patterns, base_dir):
        try:
            st = f.stat()
            fps[str(f)] = (_hash_file(f), st.st_mtime_ns)
        except OSError:
            fps[str(f)] = ("", 0.0)
    return fps


def outputs_exist(patterns: List[str], base_dir: str | None = None) -> bool:
    if not patterns:
        return False
    files = _expand_globs(patterns, base_dir)
    return len(files) >= len(patterns) and len(files) > 0


def _expand_outputs(patterns: List[str], base_dir: str | None = None) -> List[Path]:
    results: List[Path] = []
    seen: set[Path] = set()
    base = Path(base_dir) if base_dir else Path.cwd()
    for pat in patterns:
        p = Path(pat)
        if not p.is_absolute():
            p = base / p
        if any(ch in pat for ch in "*?["):
            parent = p.parent
            if parent.exists():
                for m in sorted(parent.rglob(p.name)):
                    rp = m.resolve()
                    if rp.is_file() and rp not in seen:
                        seen.add(rp)
                        results.append(rp)
        else:
            if p.exists() and p.is_file():
                rp = p.resolve()
                if rp not in seen:
                    seen.add(rp)
                    results.append(rp)
    return results


def fingerprint_outputs(patterns: List[str], base_dir: str | None = None) -> Dict[str, Tuple[str, float]]:
    fps: Dict[str, Tuple[str, float]] = {}
    for f in _expand_outputs(patterns, base_dir):
        try:
            st = f.stat()
            fps[str(f)] = (_hash_file(f), st.st_mtime_ns)
        except OSError:
            fps[str(f)] = ("", 0.0)
    return fps


def is_up_to_date(
    input_fingerprints: Dict[str, Tuple[str, float]],
    output_fingerprints: Dict[str, Tuple[str, float]],
    previous_inputs: Dict[str, Tuple[str, float]] | None,
    previous_outputs: Dict[str, Tuple[str, float]] | None,
    output_patterns: List[str],
    base_dir: str | None = None,
) -> bool:
    if not output_patterns:
        return False
    if not outputs_exist(output_patterns, base_dir):
        return False
    if previous_outputs is None:
        return False
    if not previous_outputs:
        return False
    if set(output_fingerprints.keys()) != set(previous_outputs.keys()):
        return False
    for k, (h, mt) in previous_outputs.items():
        cur = output_fingerprints.get(k)
        if cur is None or cur[0] != h:
            return False
    if previous_inputs is None:
        return True
    if not previous_inputs and not input_fingerprints:
        return True
    if set(input_fingerprints.keys()) != set(previous_inputs.keys()):
        return False
    for k, (h, mt) in previous_inputs.items():
        cur = input_fingerprints.get(k)
        if cur is None or cur[0] != h:
            return False
    return True
