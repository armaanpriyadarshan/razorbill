"""Background documents for note generation and questions.

`context_dirs` in the config points at directories of Markdown or text files
(a company knowledge base, project docs). When their total size is small they
are injected whole; otherwise a selection call shows the model an index of
the files and asks which ones are relevant to the task at hand, and only
those are injected. Failures degrade to "no context", never to a crash.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import openai_api
from .config import Config

EXTS = {".md", ".txt"}
INJECT_ALL_UNDER = 40_000   # chars: below this, skip selection and send everything
MAX_CONTEXT_CHARS = 60_000  # cap on injected document text
MAX_SELECTED = 6

SELECT_SYSTEM = """\
You select background documents relevant to a task. You are given a numbered
index of files and a task. Reply with the numbers of the files most useful
for the task, one number per line, at most {n} lines. Reply with nothing if
none are relevant.
"""


def files(cfg: Config) -> list[Path]:
    out: list[Path] = []
    for d in cfg.context_dirs:
        root = Path(d).expanduser()
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*")):
            if f.suffix.lower() in EXTS and f.is_file() and not any(
                part.startswith(".") for part in f.relative_to(root).parts
            ):
                out.append(f)
    return out


def _title(path: Path) -> str:
    try:
        for line in path.read_text(errors="replace").splitlines()[:5]:
            line = line.strip().lstrip("# ").strip()
            if line:
                return line[:80]
    except OSError:
        pass
    return ""


def _concat(paths: list[Path], limit: int = MAX_CONTEXT_CHARS) -> str:
    parts, used = [], 0
    for p in paths:
        try:
            text = p.read_text(errors="replace").strip()
        except OSError:
            continue
        take = text[: max(0, limit - used)]
        if not take:
            break
        parts.append(f"--- {p.name} ---\n{take}")
        used += len(take)
    return "\n\n".join(parts)


def gather(cfg: Config, api: openai_api.Api, purpose: str) -> str:
    """Return background-document text relevant to `purpose`, or ""."""
    candidates = files(cfg)
    if not candidates:
        return ""
    sizes = {p: p.stat().st_size for p in candidates}
    if sum(sizes.values()) <= INJECT_ALL_UNDER:
        return _concat(candidates)

    listing = "\n".join(
        f"{i}: {p.name}  {_title(p)}" for i, p in enumerate(candidates)
    )
    try:
        reply = openai_api.chat(
            cfg, api,
            SELECT_SYSTEM.format(n=MAX_SELECTED),
            f"Files:\n{listing}\n\nTask:\n{purpose[:2000]}",
        )
        picks = [int(m) for m in re.findall(r"\d+", reply)][:MAX_SELECTED]
        chosen = [candidates[i] for i in picks if 0 <= i < len(candidates)]
    except (openai_api.ApiError, ValueError):
        return ""
    return _concat(chosen)
