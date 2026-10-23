"""Persistent per-repo state (last built SHA, last result) in a JSON file."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                self._data = {}

    def acquire_lock(self) -> bool:
        """Take an exclusive lock so two instances never share one state file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(self.path.with_suffix(".lock"), "w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            return False
        return True

    def get(self, name: str) -> dict:
        with self._lock:
            return dict(self._data.get(name, {}))

    def all(self) -> dict[str, dict]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def update(self, name: str, **fields) -> None:
        with self._lock:
            self._data.setdefault(name, {}).update(fields)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
            os.replace(tmp, self.path)
