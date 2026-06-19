"""Incremental execution: input fingerprinting + output presence check."""
from __future__ import annotations

import glob
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional


@dataclass
class Fingerprint:
    inputs: Dict[str, Dict[str, float]]  # path -> {"hash": str, "mtime": float, "size": int}
    commands_hash: str

    def to_dict(self) -> Dict:
        return {"inputs": self.inputs, "commands_hash": self.commands_hash}

    @classmethod
    def from_dict(cls, data: Dict) -> "Fingerprint":
        return cls(
            inputs=data.get("inputs", {}),
            commands_hash=data.get("commands_hash", ""),
        )


def _expand_patterns(patterns: Iterable[str], base_dir: Path) -> List[Path]:
    found: List[Path] = []
    for pat in patterns:
        p = Path(pat)
        if not p.is_absolute():
            p = base_dir / p
        # glob supports recursive ** when recursive=True
        for match in glob.glob(str(p), recursive=True):
            mp = Path(match)
            if mp.is_file():
                found.append(mp.resolve())
    # de-dup, stable order
    seen = set()
    unique: List[Path] = []
    for p in sorted(found):
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _hash_file(path: Path, chunk_size: int = 65536) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            data = f.read(chunk_size)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def compute_fingerprint(
    inputs: Iterable[str],
    commands: Iterable[str],
    base_dir: Path,
) -> Fingerprint:
    files = _expand_patterns(inputs, base_dir)
    info: Dict[str, Dict[str, float]] = {}
    for f in files:
        try:
            stat = f.stat()
        except OSError:
            continue
        info[str(f)] = {
            "hash": _hash_file(f),
            "mtime": stat.st_mtime,
            "size": stat.st_size,
        }
    cmd_hash = hashlib.sha256("\n".join(commands).encode("utf-8")).hexdigest()
    return Fingerprint(inputs=info, commands_hash=cmd_hash)


def outputs_exist(outputs: Iterable[str], base_dir: Path) -> bool:
    patterns = list(outputs)
    if not patterns:
        return False
    for pat in patterns:
        p = Path(pat)
        if not p.is_absolute():
            p = base_dir / p
        if any(True for _ in glob.iglob(str(p), recursive=True)):
            continue
        return False
    return True


def is_up_to_date(
    current: Fingerprint,
    cached: Optional[Fingerprint],
    outputs: Iterable[str],
    base_dir: Path,
) -> bool:
    outputs = list(outputs)
    if not outputs:
        return False
    if cached is None:
        return False
    if cached.commands_hash != current.commands_hash:
        return False
    if set(cached.inputs.keys()) != set(current.inputs.keys()):
        return False
    for path, meta in current.inputs.items():
        prev = cached.inputs.get(path)
        if not prev:
            return False
        if prev.get("hash") != meta.get("hash"):
            return False
    if not outputs_exist(outputs, base_dir):
        return False
    return True


class FingerprintCache:
    def __init__(self, path: Path):
        self.path = path
        self._data: Dict[str, Dict] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._data = {}

    def get(self, task: str) -> Optional[Fingerprint]:
        d = self._data.get(task)
        if not d:
            return None
        return Fingerprint.from_dict(d)

    def set(self, task: str, fp: Fingerprint) -> None:
        self._data[task] = fp.to_dict()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
