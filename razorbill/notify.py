"""Desktop notifications: best-effort, never fatal.

Linux uses notify-send; macOS uses osascript. Windows has no lightweight
stdlib path to toasts, so notifications are skipped there and the TUI,
statusline, and logs carry the state instead.

`notify_action` (a notification with a clickable action) is Linux-only and
blocks until the notification closes, so call it from a worker thread. On
other platforms it degrades to a plain notification and returns False.
"""

from __future__ import annotations

import subprocess
import sys

APP = "--app-name=razorbill"


def notify(enabled: bool, title: str, body: str = "") -> None:
    if not enabled:
        return
    try:
        if sys.platform == "linux":
            subprocess.Popen(
                ["notify-send", APP, title, body],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif sys.platform == "darwin":
            script = f'display notification "{_esc(body)}" with title "{_esc(title)}"'
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except OSError:
        pass


def notify_action(enabled: bool, title: str, body: str, action: str, label: str,
                  wait_seconds: int = 600) -> bool:
    """Show a notification with one action; return True if the user chose it."""
    if not enabled:
        return False
    if sys.platform != "linux":
        notify(enabled, title, body)
        return False
    try:
        out = subprocess.run(
            ["notify-send", APP, f"--action={action}={label}", title, body],
            capture_output=True, text=True, timeout=wait_seconds,
        )
        return out.stdout.strip() == action
    except (OSError, subprocess.TimeoutExpired):
        return False


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')
