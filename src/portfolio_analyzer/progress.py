"""Terminal-only, content-free progress reporting for long local analysis work."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class AnalysisProgressReporter:
    """Emit controlled operational messages without persisting analyzed content."""

    verbose: bool = False
    sink: Callable[[str], None] | None = None

    @property
    def enabled(self) -> bool:
        return self.sink is not None

    def basic(self, message: str) -> None:
        if self.sink is not None:
            self.sink(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def detail(self, message: str) -> None:
        if self.verbose:
            self.basic(message)


@dataclass(slots=True)
class GenerationHeartbeat:
    """Report liveness while synchronous Transformers generation is running."""

    reporter: AnalysisProgressReporter
    generation_id: int
    interval_seconds: float = 15.0
    _started: float = field(default=0.0, init=False)
    _generated_tokens: int = field(default=0, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def start(self) -> None:
        self._started = time.monotonic()
        if not self.reporter.verbose:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"qwen-heartbeat-{self.generation_id}",
            daemon=True,
        )
        self._thread.start()

    def update(self, generated_tokens: int) -> None:
        with self._lock:
            self._generated_tokens = max(self._generated_tokens, generated_tokens)

    def finish(self) -> tuple[int, float]:
        elapsed = max(0.0, time.monotonic() - self._started)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(1.0, self.interval_seconds))
        with self._lock:
            generated = self._generated_tokens
        return generated, elapsed

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            elapsed = max(0.0, time.monotonic() - self._started)
            with self._lock:
                generated = self._generated_tokens
            rate = generated / elapsed if elapsed > 0 else 0.0
            self.reporter.detail(
                f"Generation {self.generation_id}: running; elapsed={elapsed:.1f}s; "
                f"generated_tokens={generated}; speed={rate:.2f} tokens/s"
            )


class TokenProgressCriteria:
    """Transformers-compatible non-stopping callback that counts output tokens."""

    def __init__(self, prompt_tokens: int, heartbeat: GenerationHeartbeat) -> None:
        self.prompt_tokens = prompt_tokens
        self.heartbeat = heartbeat

    def __call__(self, input_ids: object, _scores: object, **_kwargs: object) -> bool:
        try:
            shape = input_ids.shape  # type: ignore[attr-defined]
            total_tokens = int(shape[-1])
        except (AttributeError, IndexError, TypeError, ValueError):
            return False
        self.heartbeat.update(max(0, total_tokens - self.prompt_tokens))
        return False
