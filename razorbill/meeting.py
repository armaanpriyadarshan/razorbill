"""Post-meeting pipeline: transcribe segments, generate notes, write Markdown."""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import openai_api, transcript
from .config import Config

META = "meta.json"
JOTS = "jots.md"


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


def _transcribe_channel(cfg: Config, api: openai_api.Api, files: list[Path]) -> list[dict]:
    def one(item: tuple[int, Path]) -> list[dict]:
        idx, path = item
        return openai_api.transcribe(cfg, api, path, offset=idx * cfg.segment_seconds)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, enumerate(files)))
    return [seg for chunk in results for seg in chunk]


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


def process(cfg: Config, api: openai_api.Api, d: Path) -> Path:
    """Turn a recorded meeting directory into a Markdown note. Returns its path.

    On failure the directory is left in .pending for `razorbill reprocess`.
    """
    meta = read_meta(d)
    meta |= {"status": "processing", "processing_started": dt.datetime.now().isoformat(timespec="seconds")}
    write_meta(d, meta)
    try:
        return _process(cfg, api, d, meta)
    except Exception:
        meta["status"] = "recorded"  # release the claim so reprocess can retry
        write_meta(d, meta)
        raise


def _process(cfg: Config, api: openai_api.Api, d: Path, meta: dict) -> Path:
    me = _transcribe_channel(cfg, api, _segments(d, "me"))
    them = _transcribe_channel(cfg, api, _segments(d, "them"))
    me = transcript.dedupe_echo(me, them)  # speaker bleed when not on headphones
    utterances = transcript.merge(me, them)
    transcript_md = transcript.to_markdown(utterances)

    jots = (d / JOTS).read_text().strip() if (d / JOTS).exists() else ""

    title, notes_md = "Untitled meeting", ""
    if utterances:
        user_msg = ""
        if jots:
            user_msg += f"My own notes taken during the meeting:\n{jots}\n\n"
        user_msg += f"Transcript:\n\n{transcript_md}"
        notes_md = openai_api.chat(cfg, api, cfg.prompt(), user_msg)
        m = re.match(r"#\s+(.+)", notes_md)
        if m:
            title = m.group(1).strip()
            notes_md = notes_md[m.end():].strip()
    else:
        notes_md = "_No speech detected._"

    started = meta.get("started", d.name)
    day, clock = started[:10], started[11:16]
    front = "\n".join(
        [
            "---",
            f"title: {json.dumps(title)}",
            f"date: {day} {clock}",
            f"duration_minutes: {_duration_minutes(meta)}",
            f"app: {json.dumps(meta.get('app', 'unknown'))}",
            f"models: {cfg.transcribe_model} + {cfg.notes_model}",
            "---",
        ]
    )
    body = [front, f"# {title}", notes_md]
    if jots:
        body += ["## My notes", jots]
    body += ["## Transcript", transcript_md or "_empty_"]

    out = cfg.out_dir() / f"{d.name[:15]}-{_slug(title)}.md"  # YYYY-MM-DD-HHMM
    out.write_text("\n\n".join(body) + "\n")

    meta["status"] = "done"
    write_meta(d, meta)
    if cfg.keep_audio:
        # keep the directory but mark it done so reprocess skips it
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


def pending(cfg: Config) -> list[Path]:
    """Directories awaiting (re)processing: recorded, or claimed by a run that died."""
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
            if stale:
                out.append(d)
    return out


def discard(d: Path) -> None:
    shutil.rmtree(d, ignore_errors=True)
