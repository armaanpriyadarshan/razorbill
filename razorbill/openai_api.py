"""Minimal OpenAI-compatible API client (stdlib only: urllib + hand-rolled multipart)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Config

RETRYABLE = {429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    pass


@dataclass
class Api:
    """Resolved endpoints. Transcription and notes may use different providers."""

    transcribe_base: str
    transcribe_key: str
    notes_base: str
    notes_key: str


def resolve(cfg: Config) -> Api:
    main = cfg.resolve_key()
    api = Api(
        transcribe_base=cfg.transcribe_api_base or cfg.api_base,
        transcribe_key=cfg.transcribe_api_key or main,
        notes_base=cfg.notes_api_base or cfg.api_base,
        notes_key=cfg.notes_api_key or main,
    )
    if not (api.transcribe_key and api.notes_key):
        raise ApiError(
            "No API key. Set OPENAI_API_KEY, or api_key / api_key_command in the config."
        )
    return api


def check_key(base: str, key: str, timeout: int = 20) -> str | None:
    """Verify a key against the API's model list. Returns None on success,
    otherwise a short human-readable error."""
    req = urllib.request.Request(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "That key was rejected (401). Check for typos or an expired key."
        return f"The API answered HTTP {e.code}."
    except (urllib.error.URLError, TimeoutError) as e:
        return f"Could not reach {base}: {e}"


def _post(url: str, key: str, body: bytes, content_type: str, timeout: int = 600):
    headers = {"Authorization": f"Bearer {key}", "Content-Type": content_type}
    last = "no attempts made"
    for attempt in range(4):
        if attempt:
            time.sleep(2 ** attempt)
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:1000]
            last = f"HTTP {e.code} from {url}: {detail}"
            if e.code not in RETRYABLE:
                raise ApiError(last) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = f"{url}: {e}"
    raise ApiError(last)


def _multipart(fields: dict[str, str], file_name: str, file_bytes: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def transcribe(cfg: Config, api: Api, path: Path, offset: float) -> list[dict]:
    """Transcribe one audio segment; return utterance dicts with absolute times.

    Supports both response shapes that carry segment timestamps:
    - verbose_json (whisper-1, Groq-hosted Whisper, faster-whisper servers)
    - diarized_json (gpt-4o-transcribe-diarize), which adds per-segment speaker labels
    """
    diarize = "diarize" in cfg.transcribe_model
    fields = {
        "model": cfg.transcribe_model,
        "response_format": "diarized_json" if diarize else "verbose_json",
    }
    if diarize:
        fields["chunking_strategy"] = "auto"  # required for audio > 30s
    if cfg.language:
        fields["language"] = cfg.language
    body, ctype = _multipart(fields, path.name, path.read_bytes())
    resp = _post(f"{api.transcribe_base}/audio/transcriptions", api.transcribe_key, body, ctype)

    segments = resp.get("segments")
    if segments is None:  # model without timestamp support: one blob, no timing
        text = (resp.get("text") or "").strip()
        return [{"start": offset, "text": text, "no_speech_prob": 0.0}] if text else []
    out = []
    for s in segments:
        seg = {
            "start": offset + float(s.get("start", 0.0)),
            "text": str(s.get("text", "")).strip(),
            "no_speech_prob": float(s.get("no_speech_prob", 0.0)),
        }
        if s.get("speaker"):
            seg["speaker"] = str(s["speaker"])
        out.append(seg)
    return out


def chat(cfg: Config, api: Api, system: str, user: str, model: str = "",
         priority: bool = False) -> str:
    payload = {
        "model": model or cfg.notes_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if priority:
        payload["service_tier"] = "priority"
    body = json.dumps(payload).encode()
    resp = _post(f"{api.notes_base}/chat/completions", api.notes_key, body, "application/json")
    try:
        return resp["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError):
        raise ApiError(f"unexpected chat response: {json.dumps(resp)[:500]}") from None
