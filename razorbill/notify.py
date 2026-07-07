"""Desktop notifications: best-effort, never fatal.

`notify_action` uses notify-send's action support (dunst: middle-click the
notification to trigger the action) and blocks until the notification closes,
so call it from a worker thread.
"""

from __future__ import annotations

import subprocess

APP = "--app-name=razorbill"


def notify(enabled: bool, title: str, body: str = "") -> None:
    if not enabled:
        return
    try:
        subprocess.Popen(
            ["notify-send", APP, title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def notify_action(enabled: bool, title: str, body: str, action: str, label: str,
                  wait_seconds: int = 600) -> bool:
    """Show a notification with one action; return True if the user chose it."""
    if not enabled:
        return False
    try:
        out = subprocess.run(
            ["notify-send", APP, f"--action={action}={label}", title, body],
            capture_output=True, text=True, timeout=wait_seconds,
        )
        return out.stdout.strip() == action
    except (OSError, subprocess.TimeoutExpired):
        return False
