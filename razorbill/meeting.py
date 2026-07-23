"""Post-meeting pipeline: transcribe segments, generate notes, write Markdown."""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import threading
from pathlib import Path

from . import audio, context, events, openai_api, transcript, video
from .config import Config

META = "meta.json"


def new_meeting_dir(cfg: Config, app: str) -> Path:
    now = dt.datetime.now()
    d = cfg.pending_dir() / now.strftime("%Y-%m-%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    write_meta(d, {"app": app, "started": now.isoformat(timespec="seconds"), "status": "recording"})
    return d


def write_meta(d: Path, meta: dict) -> None:
    (d / META).write_text(json.dumps(meta, indent=2))


def read_meta(d: Path) -> dict:
    try:
        return json.loads((d / META).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _segments(d: Path, prefix: str) -> list[Path]:
    return sorted(d.glob(f"{prefix}-*.ogg"))


SEG_CACHE = "live.json"
LIVE_MD = "live.md"


def load_seg_cache(d: Path) -> dict:
    """Per-segment transcription results, keyed by file name. Shared between
    the live pass and final processing so no segment is transcribed twice."""
    try:
        return json.loads((d / SEG_CACHE).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_seg_cache(d: Path, cache: dict) -> None:
    (d / SEG_CACHE).write_text(json.dumps(cache))


def completed_segments(d: Path) -> list[Path]:
    """Segment files ffmpeg has finished writing (all but the newest per channel)."""
    out: list[Path] = []
    for prefix in ("me", "them"):
        files = _segments(d, prefix)
        out.extend(files[:-1])
    return out


def segment_offset(cfg: Config, path: Path) -> float:
    return int(path.stem.split("-")[1]) * cfg.segment_seconds


def live_markdown(cache: dict) -> str:
    me = [s for name, segs in cache.items() if name.startswith("me-") for s in segs]
    them = [s for name, segs in cache.items() if name.startswith("them-") for s in segs]
    me = transcript.dedupe_echo(me, them)
    return transcript.to_markdown(transcript.merge(me, them))


def _transcribe_channel(cfg: Config, api: openai_api.Api, files: list[Path],
                        cache: dict) -> list[dict]:
    # Plain daemon threads, not ThreadPoolExecutor: executor workers are
    # non-daemon and concurrent.futures joins them at interpreter exit, so
    # a daemon shutdown during transcription hung until systemd's SIGKILL.
    results: list[list[dict] | None] = [None] * len(files)
    errors: list[Exception] = []
    gate = threading.Semaphore(8)

    def one(idx: int, path: Path) -> None:
        with gate:
            try:
                segs = cache.get(path.name)
                if segs is None:
                    segs = openai_api.transcribe(cfg, api, path,
                                                 offset=idx * cfg.segment_seconds)
                    cache[path.name] = segs
                results[idx] = segs
            except Exception as e:
                errors.append(e)

    threads = [threading.Thread(target=one, args=(i, p), daemon=True)
               for i, p in enumerate(files)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]
    return [seg for chunk in results if chunk for seg in chunk]


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:48] or "meeting"


def _duration_minutes(meta: dict) -> int:
    try:
        start = dt.datetime.fromisoformat(meta["started"])
        end = dt.datetime.fromisoformat(meta["ended"])
        return max(1, round((end - start).total_seconds() / 60))
    except (KeyError, ValueError):
        return 0


MIN_SPEECH_SECONDS = 10  # under this much audible audio, nobody showed up


def _no_show(d: Path) -> bool:
    """Whether the recording has essentially no audible audio on either
    channel. Measured locally, before any API spend. A live transcript
    with content proves speech without decoding anything."""
    live = d / LIVE_MD
    try:
        if live.exists() and "**[" in live.read_text():
            return False
    except OSError:
        pass
    try:
        segs = _segments(d, "me") + _segments(d, "them")
        return audio.speech_seconds(segs) < MIN_SPEECH_SECONDS
    except Exception:
        return False  # analysis is best-effort; never block processing


def process(cfg: Config, api: openai_api.Api, d: Path) -> Path | None:
    """Turn a recorded meeting directory into a Markdown note. Returns its
    path, or None when the meeting was autotrashed as a no-show.

    On failure the directory is left in .pending for `razorbill reprocess`.
    """
    meta = read_meta(d)
    meta |= {"status": "processing", "processing_started": dt.datetime.now().isoformat(timespec="seconds")}
    write_meta(d, meta)
    try:
        if cfg.autotrash and _no_show(d):
            discard(d)
            return None
        return _process(cfg, api, d, meta)
    except Exception:
        meta["status"] = "recorded"  # release the claim so reprocess can retry
        write_meta(d, meta)
        raise


def _process(cfg: Config, api: openai_api.Api, d: Path, meta: dict) -> Path:
    cache = load_seg_cache(d)  # live-mode results are reused, not re-billed
    me = _transcribe_channel(cfg, api, _segments(d, "me"), cache)
    them = _transcribe_channel(cfg, api, _segments(d, "them"), cache)
    save_seg_cache(d, cache)
    me = transcript.dedupe_echo(me, them)  # speaker bleed when not on headphones
    utterances = transcript.merge(me, them)
    transcript_md = transcript.to_markdown(utterances)

    event_block = events.describe(events.read_event(d))

    title, notes_md = "Untitled meeting", ""
    if utterances:
        user_msg = ""
        if event_block:
            user_msg += (f"Calendar event for this meeting (use it for the title "
                         f"and participant names):\n{event_block}\n\n")
        if cfg.context_dirs:
            try:
                purpose = f"{event_block}\n\n{transcript_md[:3000]}" if event_block \
                    else transcript_md[:3000]
                docs = context.gather(cfg, api, purpose)
            except Exception:
                docs = ""  # background docs are best-effort, never fatal
            if docs:
                user_msg += f"Background documents (context, not meeting content):\n\n{docs}\n\n"
        user_msg += f"Transcript:\n\n{transcript_md}"
        notes_md = openai_api.chat(cfg, api, cfg.prompt(), user_msg)
        m = re.match(r"#\s+(.+)", notes_md)
        if m:
            title = m.group(1).strip()
            notes_md = notes_md[m.end():].strip()
    else:
        notes_md = "_No speech detected._"

    out = cfg.out_dir() / f"{d.name[:15]}-{_slug(title)}.md"  # YYYY-MM-DD-HHMM
    # Move the screen recording out before the cleanup below can delete it.
    # A crash between this move and the "done" status makes a reprocess run
    # regenerate the note, possibly under a different slug; the shared
    # timestamp prefix keeps note and video sorted together regardless.
    video_name = _move_video(d, out.with_suffix(".mkv"))

    started = meta.get("started", d.name)
    day, clock = started[:10], started[11:16]
    front_lines = [
        "---",
        f"title: {json.dumps(title)}",
        f"date: {day} {clock}",
        f"duration_minutes: {_duration_minutes(meta)}",
        f"app: {json.dumps(meta.get('app', 'unknown'))}",
        f"models: {cfg.transcribe_model} + {cfg.notes_model}",
    ]
    if video_name:
        front_lines.append(f"video: {video_name}")
    front_lines.append("---")
    body = ["\n".join(front_lines), f"# {title}", notes_md]
    body += ["## Transcript", transcript_md or "_empty_"]

    out.write_text("\n\n".join(body) + "\n")

    meta["status"] = "done"
    write_meta(d, meta)
    if cfg.keep_audio or (d / video.VIDEO_FILE).exists():
        # keep the directory: the user wants the audio, or a failed video
        # move left the only copy here. Marked done so reprocess skips it.
        pass
    else:
        shutil.rmtree(d)
    return out


def read_frontmatter(text: str) -> dict:
    """Parse the YAML-ish frontmatter razorbill itself writes (flat key: value)."""
    meta: dict = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        for line in text[3:end].splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"')
    return meta


def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def audio_bytes(d: Path) -> int:
    """Total recorded segment bytes. Near zero means the capture source
    delivered no data (dead PipeWire node, e.g. stale echo-cancel after
    suspend), since even silence encodes to steady Opus output."""
    return sum(f.stat().st_size for f in d.glob("*.ogg"))


def _move_video(d: Path, dest: Path) -> str:
    """Finish the screen recording: mux the meeting audio into it and put
    the result next to the note, same stem. Falls back to moving the
    silent capture when the mux fails. Returns the file name, or "" when
    there is no video or nothing could be moved (the meeting directory is
    then kept so the file is never lost)."""
    src = d / video.VIDEO_FILE
    try:
        if not src.exists() or src.stat().st_size == 0:
            return ""
        if video.mux(d, _segments(d, "me"), _segments(d, "them"), dest):
            src.unlink()
            return dest.name
        shutil.move(str(src), str(dest))  # silent video beats no video
        return dest.name
    except OSError:
        return ""


def delete_note(cfg: Config, path: Path) -> Path:
    """Move a note to .trash inside the output directory and return the new
    path. A move rather than an unlink: the audio behind a note is usually
    already deleted, so this is the last copy."""
    trash = cfg.out_dir() / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / path.name
    n = 1
    while dest.exists():
        dest = trash / f"{path.stem}-{n}{path.suffix}"
        n += 1
    path.rename(dest)
    return dest


def list_notes(cfg: Config) -> list[dict]:
    """Index of finished notes, newest first: path, title, date, minutes, app."""
    root = cfg.out_dir()
    if not root.exists():
        return []
    out = []
    for f in sorted(root.glob("*.md"), reverse=True):
        try:
            meta = read_frontmatter(f.read_text()[:600])
        except OSError:
            continue
        out.append(
            {
                "path": f,
                "title": meta.get("title", f.stem),
                "date": meta.get("date", ""),
                "minutes": meta.get("duration_minutes", ""),
                "app": meta.get("app", ""),
            }
        )
    return out


STALE_CLAIM_SECONDS = 1800


def pending(cfg: Config, all_claims: bool = False) -> list[Path]:
    """Directories awaiting (re)processing: recorded, or claimed by a run that
    died. With `all_claims`, every processing claim counts as dead, whatever
    its age; the daemon uses this at boot, when no other worker can be
    holding one (a shutdown or crash mid-processing left it behind)."""
    root = cfg.pending_dir()
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        meta = read_meta(d)
        status = meta.get("status")
        if status == "recorded":
            out.append(d)
        elif status == "processing":
            try:
                claimed = dt.datetime.fromisoformat(meta["processing_started"])
                stale = (dt.datetime.now() - claimed).total_seconds() > STALE_CLAIM_SECONDS
            except (KeyError, ValueError):
                stale = True
            if stale or all_claims:
                out.append(d)
    return out


def discard(d: Path) -> None:
    shutil.rmtree(d, ignore_errors=True)
