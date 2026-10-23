"""Scheduler + single build worker. Polls repos and handles triggered builds."""

from __future__ import annotations

import logging
import queue
import threading
import time

from . import docker_ops
from .builder import Builder
from .config import Config

log = logging.getLogger(__name__)


class Service:
    def __init__(self, config: Config, builder: Builder):
        self.config = config
        self.builder = builder
        self._queue: queue.Queue[tuple[str, bool]] = queue.Queue()
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._next_poll = {r.name: 0.0 for r in config.repos}

    def enqueue(self, name: str, force: bool = False) -> bool:
        """Queue a check for repo `name`. Duplicate requests are coalesced."""
        if self.config.repo(name) is None:
            return False
        with self._lock:
            if name in self._pending and not force:
                return True
            self._pending.add(name)
        self._queue.put((name, force))
        return True

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(("", False))

    def run(self) -> None:
        for reg in self.config.registries:
            try:
                docker_ops.login(reg)
            except Exception as e:  # noqa: BLE001
                log.error("login to %s failed: %s", reg.server, e)

        worker = threading.Thread(target=self._worker, name="build-worker", daemon=True)
        worker.start()
        log.info("watching %d repo(s): %s", len(self.config.repos),
                 ", ".join(f"{r.name}@{r.branch}" for r in self.config.repos))
        while not self._stop.is_set():
            now = time.monotonic()
            for repo in self.config.repos:
                if now >= self._next_poll[repo.name]:
                    self._next_poll[repo.name] = now + (repo.poll_interval or self.config.settings.poll_interval)
                    self.enqueue(repo.name)
            self._stop.wait(1)
        worker.join(timeout=5)

    def _worker(self) -> None:
        while not self._stop.is_set():
            name, force = self._queue.get()
            if not name:
                break
            with self._lock:
                self._pending.discard(name)
            repo = self.config.repo(name)
            try:
                self.builder.check(repo, force=force)
            except Exception:  # noqa: BLE001 - never let the worker die
                log.exception("[%s] unexpected error", name)
