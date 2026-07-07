"""Audio capture and meeting detection.

Platform support:
- Linux (PipeWire/PulseAudio): automatic meeting detection via pactl,
  echo cancellation, default-device discovery.
- macOS (avfoundation) and Windows (dshow): manual recording. Devices are
  named explicitly in the config (`source`, `sink`); those input APIs have
  no portable "default microphone" alias ffmpeg could use.

Everything records through ffmpeg into segmented 16 kHz mono Opus files.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import Config

PLATFORM = "linux" if sys.platform == "linux" else "mac" if sys.platform == "darwin" else "windows"

EC_SOURCE = "razorbill_ec_source"
EC_SINK = "razorbill_ec_sink"


# --- pactl helpers (Linux) ----------------------------------------------------

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


def _prop(props: dict, key: str) -> str:
    return str(props.get(key, "")).strip('"')


# --- device selection -----------------------------------------------------------

def default_source(cfg: Config) -> str:
    """The microphone device for the "me" channel."""
    if cfg.source:
        return cfg.source
    if PLATFORM == "linux":
        return _pactl_line("get-default-source")
    return ""  # macOS/Windows: must be configured explicitly


def default_monitor(cfg: Config) -> str:
    """The system-audio device for the "them" channel. Empty disables it."""
    if PLATFORM == "linux":
        sink = cfg.sink or _pactl_line("get-default-sink")
        return f"{sink}.monitor"
    return cfg.sink  # macOS: a loopback device such as BlackHole; Windows: a capture device


def _input_args(device: str) -> list[str]:
    """ffmpeg input arguments for one capture device on this platform."""
    if PLATFORM == "linux":
        # "-name razorbill" tags our capture streams so the detector's ignore
        # list catches them; the daemon also excludes them by PID.
        return ["-f", "pulse", "-name", "razorbill", "-i", device]
    if PLATFORM == "mac":
        return ["-f", "avfoundation", "-i", f":{device}"]
    return ["-f", "dshow", "-i", f"audio={device}"]


# --- meeting detection (Linux only) -----------------------------------------------

def detection_supported() -> bool:
    return PLATFORM == "linux"


def mic_capture_apps(cfg: Config, exclude_pids: set[int] | None = None) -> list[str]:
    """Names of foreign apps currently recording from a real mic (not a monitor).

    Meeting apps (Zoom, Meet or Teams in a browser, Slack huddles, Discord)
    hold the microphone open for the length of the call, so a foreign capture
    stream is the meeting signal. Returns [] on platforms without detection.
    """
    if not detection_supported():
        return []
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


# --- echo cancellation (Linux only) -------------------------------------------------

class EchoCancel:
    """Manage PipeWire/PulseAudio's echo-cancel module so recording without
    headphones does not feed the speakers back into the mic.

    On enable: load module-echo-cancel against the real mic/speakers, then make
    the cancelled pair the system defaults so meeting apps route through it.
    On disable: restore the previous defaults and unload the module.
    No-op on platforms other than Linux.
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
        if PLATFORM != "linux":
            return False
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


# --- recording -------------------------------------------------------------------

def mixed_pcm(source: str, monitor: str, rate: int = 24000) -> subprocess.Popen:
    """One ffmpeg mixing mic and system audio to mono pcm16 on stdout, for
    streaming transcription. normalize=0 keeps both inputs at full amplitude
    (the default halves each, starving voice detection of signal)."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *_input_args(source)]
    if monitor:
        cmd += [*_input_args(monitor),
                "-filter_complex", "amix=inputs=2:duration=longest:normalize=0"]
    cmd += ["-ac", "1", "-ar", str(rate), "-f", "s16le", "pipe:1"]
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def stereo_pcm(source: str, monitor: str, rate: int = 24000) -> subprocess.Popen:
    """One ffmpeg with mic on the left channel and system audio on the right,
    stereo pcm16 on stdout. Keeping the sides on separate channels lets a
    multichannel transcription API attribute speech to Me vs Them exactly."""
    fc = (f"[0:a]aresample={rate},aformat=channel_layouts=mono[L];"
          f"[1:a]aresample={rate},aformat=channel_layouts=mono[R];"
          "[L][R]join=inputs=2:channel_layout=stereo[out]")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           *_input_args(source), *_input_args(monitor),
           "-filter_complex", fc, "-map", "[out]",
           "-ar", str(rate), "-f", "s16le", "pipe:1"]
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


class Recorder:
    """One ffmpeg process per channel: mic ("me") and system audio ("them").

    Separate processes so a channel with no flowing data (a monitor of a sink
    nothing plays to yet) can never stall the other channel's recording. The
    "them" channel is skipped when no system-audio device is available.
    """

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen] = []

    def start(self, dir: Path, cfg: Config, source: str, monitor: str) -> None:
        dir.mkdir(parents=True, exist_ok=True)
        seg = str(cfg.segment_seconds)
        # segment_format_options flush_packets=1: make the inner ogg muxer
        # write pages to disk as they are produced instead of buffering
        # (a top-level -flush_packets does not reach it). On-disk size then
        # tracks reality, which the capture watchdog depends on.
        audio = ["-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k",
                 "-f", "segment", "-segment_time", seg, "-segment_format", "ogg",
                 "-segment_format_options", "flush_packets=1"]
        channels = [(source, "me")]
        if monitor:
            channels.append((monitor, "them"))
        self.procs = []
        for device, prefix in channels:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                   *_input_args(device), *audio, str(dir / f"{prefix}-%03d.ogg")]
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
