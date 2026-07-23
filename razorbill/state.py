"""Runtime state shared between the daemon, the CLI, and the web UI.

IPC is deliberately just files in $XDG_RUNTIME_DIR/razorbill: a status.json the
daemon rewrites every tick, and marker files the daemon polls for.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    d = Path(base) / "razorbill"
    d.mkdir(parents=True, exist_ok=True)
    return d


def open_path(path: Path | str) -> None:
    """Open a file with the platform's default application."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif os.name == "nt":
        os.startfile(str(path))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def read_status() -> dict:
    try:
        return json.loads((runtime_dir() / "status.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {"state": "off"}


def write_status(status: dict) -> None:
    (runtime_dir() / "status.json").write_text(json.dumps(status))


def clear_status() -> None:
    (runtime_dir() / "status.json").unlink(missing_ok=True)
    (runtime_dir() / "partial").unlink(missing_ok=True)


def write_partial(text: str) -> None:
    """The utterance currently being spoken (live streaming interims).
    Lives in the runtime dir (usually tmpfs) because it changes several
    times a second."""
    try:
        (runtime_dir() / "partial").write_text(text)
    except OSError:
        pass


def read_partial() -> str:
    try:
        return (runtime_dir() / "partial").read_text()
    except OSError:
        return ""


def request_start() -> None:
    (runtime_dir() / "start-request").touch()


def consume_start_request() -> bool:
    f = runtime_dir() / "start-request"
    if f.exists():
        f.unlink(missing_ok=True)
        return True
    return False


def active_dir() -> Path | None:
    s = read_status()
    if s.get("state") == "recording":
        return Path(s["dir"])
    return None


def request_stop() -> bool:
    d = active_dir()
    if d is None:
        return False
    (d / "stop").touch()
    return True


def request_trash() -> bool:
    """Stop the current recording and discard it: no transcription, no note."""
    d = active_dir()
    if d is None:
        return False
    (d / "trash").touch()
    return True
