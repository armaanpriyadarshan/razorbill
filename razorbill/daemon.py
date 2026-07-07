"""The always-on watcher: detect meetings, record, then process in the background."""

from __future__ import annotations

import datetime as dt
import logging
import signal
import threading
import time
from pathlib import Path

from . import ask, audio, context, deepgram, events, meeting, openai_api, realtime, state
from .config import Config
from .notify import notify, notify_action

log = logging.getLogger("razorbill")

MANUAL = "manual"


class Daemon:
    def __init__(self, cfg: Config, api: openai_api.Api) -> None:
        self.cfg = cfg
        self.api = api
        self.rec = audio.Recorder()
        self.ec = audio.EchoCancel()
        self.dir: Path | None = None
        self.app = ""
        self.started = 0.0
        self.last_mic_activity = 0.0
        self.processing = 0
        self.stop_flag = False
        self._live: threading.Thread | None = None
        self._rt: realtime.Realtime | None = None
        # copilot worker state (coalescing: one pass in flight, latest wins)
        self._coach_lock = threading.Lock()
        self._coach_running = False
        self._coach_pending = False
        self._coach_docs: str | None = None
        self._coach_docs_lines = 0
        self._partial = ""            # utterance in progress (deepgram interims)
        self._partial_kicked_words = 0
        self._watchdog_done = False   # per-recording early capture check
        self._last_wall = time.time()
        self._last_speech = 0.0       # newest live-transcript activity
        self._silence_notified = False
        self._await_mic_release = False  # after a silence stop: no new auto
                                         # recording until the mic is let go once
        self._ics_cache: list | None = None
        self._ics_fetched = 0.0

    # --- state file ----------------------------------------------------------
    def _write_status(self) -> None:
        if self.dir:
            status = {"state": "recording", "dir": str(self.dir), "app": self.app, "since": self.started}
        elif self.processing > 0:
            status = {"state": "processing"}
        else:
            status = {"state": "idle"}
        state.write_status(status)

    # --- meeting lifecycle -----------------------------------------------------
    def _start(self, app: str) -> None:
        if self.ec.active:
            # Record "them" from the real sink's monitor; it carries everything
            # (including apps pinned past the echo-cancel sink).
            source, monitor = audio.EC_SOURCE, f"{self.ec.prev_sink}.monitor"
        else:
            source, monitor = audio.default_source(self.cfg), audio.default_monitor(self.cfg)
        if not source:
            log.error("no microphone configured; set `source` in the config "
                      "(see docs/configuration.md, platform devices)")
            notify(self.cfg.notify, "razorbill: cannot record",
                   "No microphone configured. Set `source` in the config.")
            return
        self.dir = meeting.new_meeting_dir(self.cfg, app)
        self.app = app
        self.started = self.last_mic_activity = self._last_speech = time.time()
        self._watchdog_done = False
        self._silence_notified = False
        self.rec.start(self.dir, self.cfg, source, monitor)
        if self.cfg.live_transcript:
            if self.cfg.live_mode == "realtime":
                self._rt = realtime.Realtime(
                    self.cfg, self.api, source, monitor,
                    on_line=self._make_line_sink(self.dir),
                )
                self._rt.start()
            elif self.cfg.live_mode == "deepgram" and self.cfg.deepgram_api_key:
                self._rt = deepgram.DeepgramStream(
                    self.cfg, source, monitor,
                    on_line=self._make_line_sink(self.dir),
                    on_partial=self._make_partial_sink(self.dir),
                )
                self._rt.start()
        log.info("recording started (%s) -> %s", app, self.dir)
        if self.cfg.calendar_ics_url:
            threading.Thread(target=self._resolve_event, args=(self.dir,), daemon=True).start()
        threading.Thread(target=self._offer_stop, args=(app,), daemon=True).start()

    def _resolve_event(self, d: Path) -> None:
        """Look up the calendar event this recording belongs to (cached feed,
        15 min TTL) and drop it in the meeting directory for every consumer."""
        try:
            now = time.time()
            if self._ics_cache is None or now - self._ics_fetched > 900:
                self._ics_cache = events.parse(events.fetch(self.cfg.calendar_ics_url))
                self._ics_fetched = now
            event = events.current(self._ics_cache)
            if event and d.exists():
                events.write_event(d, event)
                log.info("calendar: %s (%d attendees)",
                         event["title"] or "untitled", len(event["attendees"]))
        except Exception as e:
            log.warning("calendar lookup failed: %s", e)

    def _make_line_sink(self, d: Path):
        lock = threading.Lock()

        def on_line(text: str, label: str = "") -> None:
            stamp = time.strftime("%H:%M:%S")
            head = f"[{stamp}] {label}:" if label else f"[{stamp}]"
            with lock, (d / meeting.LIVE_MD).open("a") as f:
                f.write(f"**{head}** {text}\n\n")
            self._partial = ""
            self._partial_kicked_words = 0
            self._last_speech = time.time()
            self._silence_notified = False
            if self.cfg.live_insights:
                self._coach_kick(d)

        return on_line

    def _make_partial_sink(self, d: Path):
        def on_partial(text: str) -> None:
            self._partial = text
            state.write_partial(text)
            if not text:
                self._partial_kicked_words = 0
                return
            self._last_speech = time.time()
            self._silence_notified = False
            # react mid-utterance once enough new words accumulate; the
            # coalescing worker absorbs the extra kicks
            words = text.count(" ") + 1
            if self.cfg.live_insights and words - self._partial_kicked_words >= 8:
                self._partial_kicked_words = words
                self._coach_kick(d)

        return on_partial

    # --- live copilot ---------------------------------------------------------
    # Every utterance triggers a pass; the only pacing is the model's own
    # latency. While a pass is in flight new utterances set a pending flag,
    # and the worker immediately reruns on the newest transcript when it
    # finishes. Nothing waits on a clock.

    def _coach_kick(self, d: Path) -> None:
        with self._coach_lock:
            self._coach_pending = True
            if self._coach_running:
                return
            self._coach_running = True
        threading.Thread(target=self._coach_loop, args=(d,), daemon=True).start()

    def _coach_loop(self, d: Path) -> None:
        while True:
            with self._coach_lock:
                if not self._coach_pending:
                    self._coach_running = False
                    return
                self._coach_pending = False
            try:
                md = ""
                if (d / meeting.LIVE_MD).exists():
                    md = (d / meeting.LIVE_MD).read_text()
                if self._partial:
                    md += f"**[now, being said]** {self._partial}\n\n"
                event = events.describe(events.read_event(d))
                self._insight_pass(d, md, docs=self._coach_context(md, event),
                                   event=event)
            except Exception as e:
                log.warning("copilot pass failed: %s", e)

    def _coach_context(self, transcript_md: str, event: str = "") -> str:
        """Background-document selection, cached and refreshed as the
        conversation grows so the per-utterance path is one chat call.
        The calendar event steers selection: attendee and company names
        often match brain documents before anyone says them out loud."""
        if not self.cfg.context_dirs:
            return ""
        lines = transcript_md.count("**[")
        if self._coach_docs is None or lines - self._coach_docs_lines >= 10:
            try:
                purpose = f"{event}\n\n{transcript_md[-3000:]}" if event else transcript_md[-3000:]
                self._coach_docs = context.gather(self.cfg, self.api, purpose,
                                                  limit=ask.COACH_DOC_CHARS)
                self._coach_docs_lines = lines
            except Exception as e:
                log.warning("context selection failed: %s", e)
                self._coach_docs = self._coach_docs or ""
        return self._coach_docs

    def _reload_ec(self) -> None:
        """Tear down and rebuild the echo-cancel plumbing (Linux only)."""
        if not (self.cfg.echo_cancel and audio.PLATFORM == "linux"):
            return
        self.ec.disable()
        if self.ec.enable(self.cfg):
            log.info("echo cancellation reloaded")
        else:
            log.warning("echo-cancel reload failed; recording without it")

    def _offer_silence_stop(self) -> None:
        if notify_action(self.cfg.notify, "Still recording",
                         "Nobody has spoken for 3 minutes. Meeting over?",
                         "stop", "Stop", wait_seconds=120):
            state.request_stop()

    def _offer_stop(self, app: str) -> None:
        if notify_action(self.cfg.notify, "Recording meeting",
                         f"Detected: {app}", "stop", "Stop"):
            state.request_stop()

    def _finish(self) -> None:
        d, app = self.dir, self.app
        assert d is not None
        self.rec.stop()
        self.dir = None
        if self._rt is not None:
            self._rt.shutdown()
            self._rt = None
        self._coach_docs, self._coach_docs_lines = None, 0
        self._partial, self._partial_kicked_words = "", 0
        state.write_partial("")
        if self._live is not None and self._live.is_alive():
            self._live.join(timeout=60)  # let the last live pass save its cache
        elapsed = time.time() - self.started

        # A recording with essentially no bytes means the capture source was
        # dead (the known cause: audio plumbing gone stale across suspend).
        # Discard loudly instead of writing an empty "no speech" note.
        if elapsed > 5 and meeting.audio_bytes(d) < 4096:
            log.error("capture produced no audio (%.0fs recording); discarding "
                      "and reloading echo cancel", elapsed)
            notify(self.cfg.notify, "razorbill: recording had no audio",
                   f"{app}: the capture source delivered no data, so there is "
                   f"nothing to transcribe. Audio devices are being reset; "
                   f"recording restarts automatically if the meeting is live.")
            meeting.discard(d)
            self._reload_ec()
            return

        # Auto-detected blips (mic checks) get discarded; manual recordings are
        # always kept: the user explicitly asked for them.
        if app != MANUAL and elapsed < self.cfg.min_meeting_seconds:
            log.info("discarding %.0fs auto recording (< min_meeting_seconds)", elapsed)
            notify(self.cfg.notify, "Recording discarded",
                   f"{app}: only {elapsed:.0f}s of audio, treated as a mic check. No notes.")
            meeting.discard(d)
            return

        meta = meeting.read_meta(d)
        meta |= {"ended": dt.datetime.now().isoformat(timespec="seconds"), "status": "recorded"}
        meeting.write_meta(d, meta)
        log.info("meeting ended (%s, %.0f min); processing", app, elapsed / 60)
        self.processing += 1
        # Daemon thread: shutdown must not wait on note generation (systemd
        # kills slow stops). Anything cut off is recovered at next boot.
        threading.Thread(target=self._process, args=(d,), daemon=True).start()

    def _process(self, d: Path) -> None:
        try:
            out = meeting.process(self.cfg, self.api, d)
            log.info("notes written: %s", out)
            threading.Thread(target=self._offer_open, args=(out,), daemon=True).start()
        except Exception as e:  # keep the daemon alive whatever the API does
            log.error("processing failed, audio kept for `razorbill reprocess`: %s", e)
            notify(self.cfg.notify, "razorbill: processing failed",
                   f"{e}\nAudio kept. Run: razorbill reprocess")
        finally:
            self.processing -= 1

    def _offer_open(self, path: Path) -> None:
        if notify_action(self.cfg.notify, "Meeting notes ready", str(path), "open", "Open"):
            state.open_path(path)

    # --- main loop ---------------------------------------------------------------
    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        self.cfg.out_dir().mkdir(parents=True, exist_ok=True)

        if self.cfg.echo_cancel and audio.PLATFORM == "linux":
            if self.ec.enable(self.cfg):
                log.info("echo cancellation active; no headphones needed")
            else:
                log.warning("echo-cancel module unavailable; recording without it "
                            "(wear headphones or set echo_cancel = false)")
        if (self.cfg.live_transcript and self.cfg.live_mode == "deepgram"
                and not self.cfg.deepgram_api_key):
            log.error("live_mode = \"deepgram\" needs deepgram_api_key; "
                      "live transcript is off for this run")
        # Recover meetings a previous run left unfinished: recorded but never
        # processed, or claimed by a process that no longer exists. At boot
        # no other worker can hold a claim, so every claim counts as dead.
        leftovers = meeting.pending(self.cfg, all_claims=True)
        if leftovers:
            log.info("resuming %d unfinished meeting(s) from a previous run", len(leftovers))
            for d in leftovers:
                meta = meeting.read_meta(d)
                meta["status"] = "recorded"  # release any dead claim
                meeting.write_meta(d, meta)
                self.processing += 1
                threading.Thread(target=self._process, args=(d,), daemon=True).start()

        if audio.detection_supported():
            log.info("watching for meetings (mic capture by other apps)")
        else:
            log.info("automatic detection is not available on %s; start recordings "
                     "manually (TUI, `razorbill start`, or `razorbill toggle`)",
                     audio.PLATFORM)

        while not self.stop_flag:
            now = time.time()
            if now - self._last_wall > self.cfg.poll_seconds * 2 + 60:
                # The wall clock jumped: we slept through a suspend. Audio
                # plumbing loaded before it cannot be trusted afterward
                # (a stale echo-cancel node delivers pure silence).
                log.info("resume from suspend detected; resetting audio plumbing")
                if self.dir is not None:
                    self._finish()  # its capture died with the suspend
                self._reload_ec()
            self._last_wall = now
            try:
                self._tick()
            except Exception as e:
                log.error("tick failed: %s", e)
            self._write_status()
            time.sleep(self.cfg.poll_seconds)

        if self.dir:
            self._finish()
        self.ec.disable()
        state.clear_status()
        log.info("stopped")

    def _tick(self) -> None:
        manual_start = state.consume_start_request()
        ours = self.rec.pids() | (self._rt.pids() if self._rt else set())
        apps = audio.mic_capture_apps(self.cfg, exclude_pids=ours)
        now = time.time()

        if self.dir is None:
            if manual_start:
                self._await_mic_release = False
                self._start(MANUAL)
            elif apps:
                if not self._await_mic_release:
                    self._start(apps[0])
                # else: a silence stop already ended this mic session; wait
                # for the app to release the mic before trusting it again
            else:
                self._await_mic_release = False
            return

        # recording: check the stop conditions
        if apps:
            self.last_mic_activity = now
        stop_requested = (self.dir / "stop").exists()
        auto = self.app != MANUAL
        idle_too_long = auto and (now - self.last_mic_activity) > self.cfg.grace_seconds
        over_cap = (now - self.started) > self.cfg.max_hours * 3600
        died = not self.rec.alive()

        if stop_requested or idle_too_long or over_cap or died:
            if died:
                log.error("recorder exited unexpectedly, see ffmpeg.log in %s", self.dir)
            self._finish()
            return

        # Early capture watchdog: 20 seconds in, a healthy recording has tens
        # of kilobytes of Opus on disk. Nothing at all means the source is
        # dead; _finish discards it, reloads echo cancel, and detection
        # restarts the recording within a poll or two.
        if not self._watchdog_done and now - self.started > 20:
            self._watchdog_done = True
            if meeting.audio_bytes(self.dir) < 4096:
                log.error("capture watchdog: no audio after 20s, restarting")
                self._finish()
                return

        # Silence backstop (live mode only: the stream is the speech signal).
        # Detection ignores corked (paused) mic streams, so a call's rejoin
        # page usually ends the meeting via the normal grace period; this
        # covers apps that keep actively capturing anyway.
        if self._rt is not None and self.cfg.silence_stop_minutes > 0:
            quiet = now - self._last_speech
            if not self._silence_notified and quiet > 180:
                self._silence_notified = True
                threading.Thread(target=self._offer_silence_stop, daemon=True).start()
            if quiet > self.cfg.silence_stop_minutes * 60:
                log.info("no speech for %.0f min; ending the meeting", quiet / 60)
                notify(self.cfg.notify, "Recording stopped",
                       f"No speech for {quiet / 60:.0f} minutes. Notes are on the way. "
                       f"A new recording starts when the mic is released and used again.")
                self._await_mic_release = True
                self._finish()
                return

        if (self.cfg.live_transcript and self.cfg.live_mode == "segments"
                and (self._live is None or not self._live.is_alive())):
            self._live = threading.Thread(target=self._live_pass, args=(self.dir,), daemon=True)
            self._live.start()

    def _live_pass(self, d: Path) -> None:
        """Transcribe finished segments during the meeting and, when enabled,
        run a proactive insight pass over the growing transcript."""
        try:
            cache = meeting.load_seg_cache(d)
            pending = [p for p in meeting.completed_segments(d) if p.name not in cache]
            if not pending:
                return
            for p in pending[:2]:  # bounded work per pass; the tick respawns us
                cache[p.name] = openai_api.transcribe(
                    self.cfg, self.api, p, offset=meeting.segment_offset(self.cfg, p))
            meeting.save_seg_cache(d, cache)
            md = meeting.live_markdown(cache)
            (d / meeting.LIVE_MD).write_text(md)
            if self.cfg.live_insights and md:
                self._insight_pass(d, md)
        except Exception as e:  # the final processing pass will catch up
            log.warning("live pass failed: %s", e)

    def _insight_pass(self, d: Path, transcript_md: str, docs: str | None = None,
                      event: str = "") -> None:
        insights = d / meeting.INSIGHTS_MD
        prior = insights.read_text() if insights.exists() else ""
        reply = ask.insight(self.cfg, self.api, transcript_md, prior,
                            docs=docs, event=event)
        if not reply or not d.exists():  # meeting may have ended mid-pass
            return
        stamp = dt.datetime.now().strftime("%H:%M")
        with insights.open("a") as f:
            f.write(f"[{stamp}] {reply}\n\n")
        log.info("insight: %s", reply.replace("\n", " / "))
        notify(self.cfg.notify, "razorbill", reply[:220])

    def _on_signal(self, signum, frame) -> None:  # noqa: ARG002
        self.stop_flag = True


def run(cfg: Config) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        api = openai_api.resolve(cfg)
    except openai_api.ApiError as e:
        raise SystemExit(str(e)) from None
    Daemon(cfg, api).run()
