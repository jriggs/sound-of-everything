"""Tiny stdout logger that timestamps each line and flushes immediately.

Flushing matters in CI (GitHub Actions): Python block-buffers stdout when it's
piped, so without an explicit flush the progress lines don't show up until the
whole step finishes — making a slow run look frozen. Every line here flushes, so
progress streams live.
"""

from __future__ import annotations

import time


class Logger:
    def __init__(self) -> None:
        self.start = time.time()

    def __call__(self, msg: str = "") -> None:
        print(f"[{time.time() - self.start:6.1f}s] {msg}", flush=True)


def new_logger() -> Logger:
    return Logger()
