"""PipeWire/PulseAudio detection and dual-stream recording via pactl + ffmpeg."""

from __future__ import annotations

import json
import signal
import subprocess
import time
from pathlib import Path

from .config import Config


def _pactl_json(*args: str):
    out = subprocess.run(
        ["pactl", "-f", "json", *args], capture_output=True, text=True, timeout=10
    )
    if out.returncode != 0:
        raise RuntimeError(f"pactl {' '.join(args)}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def _pactl_line(*args: str) -> str:
    return subprocess.run(
        ["pactl", *args], capture_output=True, text=True, timeout=10
    ).stdout.strip()


def default_source(cfg: Config) -> str:
    return cfg.source or _pactl_line("get-default-source")


def default_monitor(cfg: Config) -> str:
    sink = cfg.sink or _pactl_line("get-default-sink")
    return f"{sink}.monitor"


def _prop(props: dict, key: str) -> str:
    return str(props.get(key, "")).strip('"')


def mic_capture_apps(cfg: Config, exclude_pids: set[int] | None = None) -> list[str]:
    """Names of foreign apps currently recording from a real mic (not a monitor).

    Any meeting app (Zoom, Meet/Teams in a browser, Slack huddles, Discord)
    opens the microphone for the duration of the call, so "someone is capturing
    the mic" is a platform-agnostic 'meeting in progress' signal.
    `exclude_pids` filters out our own recorder processes.
    """
    monitors = {
        s["index"] for s in _pactl_json("list", "sources") if str(s.get("name", "")).endswith(".monitor")
    }
    excluded = {str(p) for p in (exclude_pids or set())}
    ignores = cfg.all_ignores()
    apps = []
    for so in _pactl_json("list", "source-outputs"):
        if so.get("source") in monitors:
            continue  # capturing system audio, not the mic
        props = so.get("properties", {})
        if _prop(props, "media.role") == "filter":
            continue  # audio-filter plumbing (e.g. echo-cancel's own capture)
        if _prop(props, "application.process.id") in excluded:
            continue
        name = _prop(props, "application.name") or _prop(props, "application.process.binary") or "unknown"
        haystack = f"{name} {_prop(props, 'media.name')}".lower()
        if any(ig in haystack for ig in ignores):
            continue
        apps.append(name)
    return apps


EC_SOURCE = "razorbill_ec_source"
EC_SINK = "razorbill_ec_sink"


class EchoCancel:
    """Manage PipeWire/PulseAudio's echo-cancel module so recording without
    headphones doesn't feed the speakers back into the mic.

    On enable: load module-echo-cancel against the real mic/speakers, then make
    the cancelled pair the system defaults so meeting apps route through it.
    On disable: restore the previous defaults and unload the module.
    """

    def __init__(self) -> None:
        self.module_id: str | None = None
        self.prev_source: str | None = None
        self.prev_sink: str | None = None

    @staticmethod
    def _stale_module_ids() -> list[str]:
        out = subprocess.run(["pactl", "list", "modules", "short"],
                             capture_output=True, text=True, timeout=10).stdout
        return [line.split("\t")[0] for line in out.splitlines() if "razorbill_ec" in line]

    def enable(self, cfg: Config) -> bool:
        try:
            for mid in self._stale_module_ids():  # e.g. left over from a crash
                subprocess.run(["pactl", "unload-module", mid], capture_output=True, timeout=10)
            mic = cfg.source or _pactl_line("get-default-source")
            sink = cfg.sink or _pactl_line("get-default-sink")
            out = subprocess.run(
                ["pactl", "load-module", "module-echo-cancel", "aec_method=webrtc",
                 f"source_master={mic}", f"sink_master={sink}",
                 f"source_name={EC_SOURCE}", f"sink_name={EC_SINK}",
                 "source_properties=device.description=razorbill-mic-echo-cancelled",
                 "sink_properties=device.description=razorbill-playback"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode != 0:
                raise RuntimeError(out.stderr.strip() or "load-module failed")
            self.module_id = out.stdout.strip()
            self.prev_source, self.prev_sink = mic, sink
            subprocess.run(["pactl", "set-default-source", EC_SOURCE], capture_output=True, timeout=10)
            subprocess.run(["pactl", "set-default-sink", EC_SINK], capture_output=True, timeout=10)
            return True
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            self.module_id = None
            return False

    def disable(self) -> None:
        if self.prev_source:
            subprocess.run(["pactl", "set-default-source", self.prev_source], capture_output=True, timeout=10)
        if self.prev_sink:
            subprocess.run(["pactl", "set-default-sink", self.prev_sink], capture_output=True, timeout=10)
        if self.module_id:
            subprocess.run(["pactl", "unload-module", self.module_id], capture_output=True, timeout=10)
        self.module_id = None

    @property
    def active(self) -> bool:
        return self.module_id is not None


class Recorder:
    """Two ffmpeg processes, mic ("me") and sink monitor ("them"), writing
    segmented 16 kHz mono Opus files, small enough for the transcription API.

    Separate processes so a channel with no flowing data (a monitor of a sink
    nothing plays to yet) can never stall the other channel's recording.
    """

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen] = []

    def start(self, dir: Path, cfg: Config, source: str, monitor: str) -> None:
        dir.mkdir(parents=True, exist_ok=True)
        seg = str(cfg.segment_seconds)
        audio = ["-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k",
                 "-f", "segment", "-segment_time", seg, "-segment_format", "ogg"]
        self.procs = []
        for input_name, prefix in ((source, "me"), (monitor, "them")):
            # "-name razorbill" tags our capture streams so the detector's
            # ignore list catches them; the daemon also excludes them by PID.
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                   "-f", "pulse", "-name", "razorbill", "-i", input_name,
                   *audio, str(dir / f"{prefix}-%03d.ogg")]
            self.procs.append(subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=(dir / f"ffmpeg-{prefix}.log").open("wb"),
            ))

    def pids(self) -> set[int]:
        return {p.pid for p in self.procs}

    def alive(self) -> bool:
        return bool(self.procs) and all(p.poll() is None for p in self.procs)

    def stop(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.send_signal(signal.SIGINT)  # lets ffmpeg finalize the files
        deadline = time.monotonic() + 5
        for p in self.procs:
            try:
                p.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()  # e.g. input never delivered data; nothing to finalize
                p.wait()
        self.procs = []
