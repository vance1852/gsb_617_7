from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


class FileFingerprint:
    __slots__ = ("path", "size", "mtime", "hash")

    def __init__(self, path: str, size: int, mtime: float, file_hash: str):
        self.path = path
        self.size = size
        self.mtime = mtime
        self.hash = file_hash

    def to_dict(self) -> Dict:
        return {
            "path": self.path,
            "size": self.size,
            "mtime": self.mtime,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "FileFingerprint":
        return cls(
            path=data["path"],
            size=data["size"],
            mtime=data["mtime"],
            file_hash=data["hash"],
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FileFingerprint):
            return NotImplemented
        return (
            self.path == other.path
            and self.size == other.size
            and self.hash == other.hash
        )


def _hash_file(filepath: Path, chunk_size: int = 65536) -> str:
    h = hashlib.sha256()
    try:
        stat = filepath.stat()
        h.update(str(stat.st_size).encode())
        h.update(str(int(stat.st_mtime_ns)).encode())
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
    except (OSError, IOError):
        return ""
    return h.hexdigest()


def _split_glob_pattern(pattern_path: Path, base: Path) -> tuple[Path, str]:
    parts = pattern_path.parts
    static_parts: list = []
    glob_parts: list = []
    found_glob = False

    for part in parts:
        if found_glob or any(ch in part for ch in "*?[]"):
            found_glob = True
            glob_parts.append(part)
        else:
            static_parts.append(part)

    if not static_parts:
        search_dir = base
    elif Path(*static_parts).is_absolute():
        search_dir = Path(*static_parts)
    else:
        search_dir = base / Path(*static_parts)

    if not glob_parts:
        glob_pattern = ""
    else:
        glob_pattern = str(Path(*glob_parts))

    return search_dir, glob_pattern


def _expand_globs(patterns: List[str], base_dir: str) -> List[Path]:
    files: Set[Path] = set()
    base = Path(base_dir).resolve()

    for pattern in patterns:
        p = Path(pattern)

        has_wildcard = any(ch in pattern for ch in "*?[]")

        if not p.is_absolute():
            p_resolved = base / p
        else:
            p_resolved = p

        if has_wildcard:
            search_dir, glob_pattern = _split_glob_pattern(p_resolved, base)
            if not search_dir.exists():
                continue
            if not glob_pattern:
                if search_dir.is_file():
                    files.add(search_dir.resolve())
                elif search_dir.is_dir():
                    for f in search_dir.rglob("*"):
                        if f.is_file():
                            files.add(f.resolve())
            else:
                for matched in search_dir.glob(glob_pattern):
                    if matched.is_file():
                        files.add(matched.resolve())
        else:
            target = p_resolved
            if target.exists() and target.is_file():
                files.add(target.resolve())
            elif target.exists() and target.is_dir():
                for f in target.rglob("*"):
                    if f.is_file():
                        files.add(f.resolve())

    return sorted(files)


def collect_fingerprints(patterns: List[str], base_dir: str) -> Dict[str, FileFingerprint]:
    result: Dict[str, FileFingerprint] = {}
    for fpath in _expand_globs(patterns, base_dir):
        try:
            stat = fpath.stat()
            fp = FileFingerprint(
                path=str(fpath),
                size=stat.st_size,
                mtime=stat.st_mtime,
                file_hash=_hash_file(fpath),
            )
            result[str(fpath)] = fp
        except (OSError, IOError):
            continue
    return result


def outputs_exist(patterns: List[str], base_dir: str) -> bool:
    if not patterns:
        return False
    files = _expand_globs(patterns, base_dir)
    return len(files) > 0


def is_up_to_date(
    task_name: str,
    input_patterns: List[str],
    output_patterns: List[str],
    base_dir: str,
    stored_input_fingerprints: Optional[Dict[str, Dict]],
) -> bool:
    if not input_patterns and not output_patterns:
        return False

    if stored_input_fingerprints is None:
        return False

    current_inputs = collect_fingerprints(input_patterns, base_dir)

    if not outputs_exist(output_patterns, base_dir):
        return False

    if len(current_inputs) != len(stored_input_fingerprints):
        return False

    for path, fp_dict in stored_input_fingerprints.items():
        if path not in current_inputs:
            return False
        stored = FileFingerprint.from_dict(fp_dict)
        current = current_inputs[path]
        if stored != current:
            return False

    return True
