"""저장 폴더 감시. 파일이 저장되면 쓰기가 끝날 때까지 기다렸다가 콜백을 부른다."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

IGNORE_PREFIXES = ("~$", ".~lock")


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: FolderWatcher) -> None:
        self.watcher = watcher

    def on_modified(self, event: FileSystemEvent) -> None:
        self.watcher.touch(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        self.watcher.touch(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        self.watcher.touch(event.dest_path)


class FolderWatcher:
    def __init__(self, folder: Path, extensions: list[str], on_saved: Callable[[Path], bool]) -> None:
        self.folder = folder
        self.extensions = {e if e.startswith(".") else f".{e}" for e in extensions}
        self.on_saved = on_saved
        self.observer = Observer()
        self.pending: dict[str, float] = {}
        self.hashes: dict[str, str] = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    def touch(self, raw_path: str | bytes) -> None:
        path = Path(raw_path.decode() if isinstance(raw_path, bytes) else raw_path)
        if path.suffix.lower() not in self.extensions:
            return
        if path.name.startswith(IGNORE_PREFIXES):
            return
        with self.lock:
            self.pending[str(path)] = time.monotonic()

    def _flush_loop(self) -> None:
        while not self.stop_event.wait(1.0):
            now = time.monotonic()
            ready: list[Path] = []
            with self.lock:
                for key, stamp in list(self.pending.items()):
                    if now - stamp >= 2.0:
                        ready.append(Path(key))
                        del self.pending[key]
            for path in ready:
                if not path.exists():
                    continue
                digest = _digest(path)
                if digest is None or self.hashes.get(str(path)) == digest:
                    continue
                self.hashes[str(path)] = digest
                self.on_saved(path)

    def start(self) -> None:
        self.observer.schedule(_Handler(self), str(self.folder), recursive=False)
        self.observer.start()
        threading.Thread(target=self._flush_loop, daemon=True).start()

    def stop(self) -> None:
        self.stop_event.set()
        self.observer.stop()
        self.observer.join(timeout=5)


def _digest(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()
