"""Einfacher Hintergrund-Job-Runner: genau ein Job (Scan oder Build) zur Zeit."""
from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

log = logging.getLogger("bora.jobs")


@dataclass
class JobState:
    name: str = ""
    running: bool = False
    message: str = "Bereit"
    percent: Optional[float] = None  # 0-100, None = unbestimmt
    started: Optional[str] = None
    finished: Optional[str] = None
    result: Any = None
    error: Optional[str] = None
    history: list[str] = field(default_factory=list)


class JobRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = JobState()
        self._queued: Optional[tuple[str, Callable[[Callable[[str], None]], Any]]] = None

    def progress(self, msg: str, percent: Optional[float] = None) -> None:
        self.state.message = msg
        self.state.percent = None if percent is None else max(0.0, min(100.0, round(percent, 1)))

    def submit(self, name: str, fn: Callable[[Callable[[str], None]], Any]) -> bool:
        with self._lock:
            if self.state.running:
                return False
            self.state = JobState(name=name, running=True, message=f"{name} gestartet",
                                  started=datetime.now().isoformat(timespec="seconds"),
                                  history=self.state.history[-20:])
        threading.Thread(target=self._run, args=(name, fn), daemon=True, name=f"job-{name}").start()
        return True

    def submit_or_queue(self, name: str, fn: Callable[[Callable[[str], None]], Any]) -> bool:
        """Starten oder - falls ein Job läuft - im Anschluss ausführen (max. ein Job wartet).
        Liefert True bei sofortigem Start."""
        with self._lock:
            if self.state.running:
                self._queued = (name, fn)
                return False
        return self.submit(name, fn)

    def _run(self, name: str, fn: Callable[[Callable[[str], None]], Any]) -> None:
        try:
            self.state.result = fn(self.progress)
            self.state.message = f"{name} abgeschlossen"
            self.state.percent = 100.0
        except Exception as exc:
            log.error("Job %s fehlgeschlagen:\n%s", name, traceback.format_exc())
            self.state.error = f"{type(exc).__name__}: {exc}"
            self.state.message = f"{name} fehlgeschlagen"
        finally:
            self.state.finished = datetime.now().isoformat(timespec="seconds")
            self.state.history.append(f"{self.state.finished} – {self.state.message}")
            with self._lock:
                self.state.running = False
                queued, self._queued = self._queued, None
            if queued:
                self.submit(*queued)

    def wait(self, timeout: float = 60) -> None:
        """Nur für Tests: warten, bis kein Job mehr läuft oder wartet."""
        while True:
            threads = [t for t in threading.enumerate() if t.name.startswith("job-")]
            if not threads:
                return
            for t in threads:
                t.join(timeout)
