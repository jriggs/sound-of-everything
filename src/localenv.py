"""Load key=value pairs from a local .env file into os.environ, if present.

No-op when there's no .env (e.g. in GitHub Actions, where credentials arrive as
real env vars / secrets). Never overrides values already set in the environment.
"""

from __future__ import annotations

import os
import pathlib


def load_dotenv(path: str | os.PathLike | None = None) -> None:
    env_path = (pathlib.Path(path) if path
                else pathlib.Path(__file__).resolve().parent.parent / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
