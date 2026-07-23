"""Meeting screen recording.

During the meeting one ffmpeg process captures the full screen, video
only, into `screen.mkv` in the meeting directory. Post-processing then
muxes the recorded audio segments into it (`mux()`), so the file that
lands next to the note is self-contained and playable.

The capture is video-only on purpose: a pulse monitor source delivers no
data while its sink is idle, and an ffmpeg that waits on such an input
stalls before writing a single frame, taking the mic and video down with
it. The screen never stalls; the audio is already safely on disk in the
segment files, which are complete by mux time.

Platform support: Linux (X11 via x11grab) and macOS (avfoundation screen
device). Windows has no video; `supported()` gates every caller. Video is
best-effort by design: a launch failure or a mid-meeting death never ends
the meeting or touches the audio recording.

Matroska container: it stays playable even when the process dies before
finalizing, which is what a crash leaves behind.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from .audio import PLATFORM
from .config import Config

VIDEO_FILE = "screen.mkv"
LOG_FILE = "ffmpeg-video.log"
MUX_LOG_FILE = "ffmpeg-mux.log"

CRF = "26"
PRESET = "veryfast"


def supported() -> bool:
    return PLATFORM in ("linux", "mac")


def _video_input(cfg: Config) -> list[str]:
    """ffmpeg input arguments for the full screen on this platform."""
    fps = str(cfg.video_fps)
    if PLATFORM == "linux":
        screen = cfg.video_screen or os.environ.get("DISPLAY") or ":0"
        return ["-f", "x11grab", "-framerate", fps, "-i", screen]
    # macOS: the screen is an avfoundation device; ":none" leaves the
    # audio slot of this input empty.
    screen = cfg.video_screen or "Capture screen 0"
    return ["-f", "avfoundation", "-capture_cursor", "1",
            "-framerate", fps, "-i", f"{screen}:none"]


class VideoRecorder:
    """Same lifecycle shape as audio.Recorder, for one screen-capture process."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None

    def start(self, dir: Path, cfg: Config) -> None:
        # The crop drops at most one pixel per axis: yuv420p needs even
        # dimensions and multi-monitor virtual screens can be odd-sized.
        # 1 s clusters + flushed packets: the matroska muxer otherwise
        # buffers ~5 s in memory, which a crash loses entirely and which
        # keeps the on-disk size from tracking reality.
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               *_video_input(cfg),
               "-vf", "crop=iw-mod(iw\\,2):ih-mod(ih\\,2):0:0",
               "-c:v", "libx264", "-preset", PRESET, "-crf", CRF,
               "-pix_fmt", "yuv420p",
               "-cluster_time_limit", "1000", "-flush_packets", "1",
               str(dir / VIDEO_FILE)]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=(dir / LOG_FILE).open("wb"),
        )

    def pids(self) -> set[int]:
        return {self.proc.pid} if self.proc else set()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def abandon(self) -> None:
        """Forget a dead process so its death is reported exactly once."""
        self.proc = None

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)  # lets ffmpeg finalize the mkv
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()  # e.g. capture never delivered; nothing to finalize
            self.proc.wait()
        self.proc = None


def mux(d: Path, me: list[Path], them: list[Path], dest: Path) -> bool:
    """Mux the meeting audio segments into the screen recording at `dest`.

    Video is stream-copied; the me/them channels are concatenated and
    mixed into one Opus track. Both recorders start together, so the
    streams line up to within a fraction of a second. Returns False when
    ffmpeg fails; the caller falls back to the silent capture.
    """
    src = d / VIDEO_FILE
    inputs: list[str] = ["-i", str(src)]
    lists: list[Path] = []
    for name, segs in (("me", me), ("them", them)):
        if not segs:
            continue
        lst = d / f"concat-{name}.txt"
        lst.write_text("".join(f"file '{p.resolve()}'\n" for p in segs))
        lists.append(lst)
        inputs += ["-f", "concat", "-safe", "0", "-i", str(lst)]
    if not lists:
        return False
    if len(lists) == 2:
        audio = ["-filter_complex",
                 "[1:a][2:a]amix=inputs=2:duration=longest:normalize=0[a]",
                 "-map", "0:v", "-map", "[a]"]
    else:
        audio = ["-map", "0:v", "-map", "1:a"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
           *audio, "-c:v", "copy", "-c:a", "libopus", "-b:a", "48k", str(dest)]
    ok = False
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=(d / MUX_LOG_FILE).open("wb"), timeout=600)
        ok = r.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    finally:
        for lst in lists:
            lst.unlink(missing_ok=True)
        if not ok:
            dest.unlink(missing_ok=True)  # never leave a broken partial file
    return ok
