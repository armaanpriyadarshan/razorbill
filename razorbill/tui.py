"""The razorbill terminal interface.

A thin client over the same files the daemon and CLI use: status comes from
$XDG_RUNTIME_DIR/razorbill/status.json, actions are marker files, notes are
Markdown on disk. The TUI runs anywhere a terminal runs, including machines
where the recording daemon itself can't (see README, platform support).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Input, Label, ListItem, ListView, Markdown, Static

from . import ask, audio, config, meeting, openai_api, state

# The project mark (assets/razorbill.png) rendered as braille by
# ascii-image-converter: a razorbill with its head tilted down.
ART = "\n".join(
    (
        "⠀⠀⠀⠀⢀⣠⣤⣶⣶⣦⣤⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
        "⠀⠀⢀⣶⣿⣿⣿⣿⣿⣿⣿⣿⣦⡀⠀⠀⠀⠀⠀⠀⠀⠀",
        "⠀⠀⣾⣿⣿⣿⣿⣿⡿⣿⣿⣿⣿⣿⣄⠀⠀⠀⠀⠀⠀⠀",
        "⠀⠀⣿⣿⣿⣿⢟⣭⣾⣿⣿⣿⣿⣿⣿⣦⠀⠀⠀⠀⠀⠀",
        "⠀⠀⢸⣿⡟⣱⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣄⠀⠀⠀⠀",
        "⠀⠀⣼⣿⣾⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣄⠀⠀",
        "⠀⢸⡇⣿⣳⠟⠁⠀⠹⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⠀",
        "⠀⢸⣿⡿⠃⠀⠀⠀⠀⠈⠉⠉⠉⠉⠉⠉⠙⠻⢿⣿⣿⠀",
        "⠀⠀⠉⠀⠀⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⢿⠀",
        "⠀⠀⠀⠀⠀⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠀",
        "⠀⠀⠀⠀⠀⢿⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
    )
)

TAGLINE = "meeting transcription and notes"


def _status() -> tuple[str, str, str]:
    """Current daemon state as (css_class, glyph, text)."""
    s = state.read_status()
    st = s.get("state")
    if st == "recording":
        mins = int((time.time() - s.get("since", time.time())) / 60)
        return "recording", "●", f"recording · {s.get('app', '?')} · {mins} min"
    if st == "processing":
        return "processing", "✎", "writing notes"
    if st == "idle":
        return "idle", "○", "waiting for a meeting"
    return "off", "◌", "daemon offline · start it with: systemctl --user start razorbill"


class NoteItem(ListItem):
    def __init__(self, note: dict) -> None:
        meta = " · ".join(x for x in (note["date"], f"{note['minutes']} min" if note["minutes"] else "", note["app"]) if x)
        super().__init__(
            Label(note["title"], classes="note-title"),
            Label(meta, classes="note-meta"),
        )
        self.path: Path = note["path"]


class NoteScreen(Screen):
    """One meeting note, rendered."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("e", "open_editor", "editor"),
        Binding("q", "app.quit", "quit"),
    ]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def compose(self) -> ComposeResult:
        try:
            text = meeting.strip_frontmatter(self.path.read_text())
        except OSError as e:
            text = f"could not read {self.path}: {e}"
        yield VerticalScroll(Markdown(text), id="note-scroll")
        yield Static("", id="flash")
        yield Footer()

    def action_open_editor(self) -> None:
        state.open_path(self.path)
        self.app.flash(f"opened {self.path.name}")


class AskScreen(Screen):
    """Ask about the live meeting or the most recent note."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("q", "app.quit", "quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="ask about the meeting (enter to send)", id="ask-input")
        yield VerticalScroll(Markdown("", id="ask-answer"), id="ask-scroll")
        yield Static("", id="flash")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#ask-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        question = event.value.strip()
        if not question:
            return
        self.query_one("#ask-answer", Markdown).update(f"**{question}**\n\nthinking ...")
        self.run_worker(lambda: self._ask(question), thread=True)

    def _ask(self, question: str) -> None:
        try:
            api = openai_api.resolve(self.app.cfg)
            reply = ask.answer(self.app.cfg, api, question)
        except Exception as e:
            reply = f"could not answer: {e}"
        self.app.call_from_thread(
            self.query_one("#ask-answer", Markdown).update, f"**{question}**\n\n{reply}"
        )


class SetupScreen(Screen):
    """First run: collect and verify the OpenAI API key."""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(ART, id="setup-art"),
            Static("razorbill needs an API key for transcription and note generation.\n"
                   "The default endpoint is api.openai.com; any OpenAI-compatible\n"
                   "endpoint works (api_base in the config). The key is stored in\n"
                   "~/.config/razorbill/config.toml with owner-only permissions.", id="setup-blurb"),
            Input(placeholder="sk-...", password=True, id="key-input"),
            Static("", id="setup-msg"),
            id="setup-box",
        )

    def on_mount(self) -> None:
        self.query_one("#key-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        key = event.value.strip()
        if not key:
            return
        msg = self.query_one("#setup-msg", Static)
        msg.update("checking the key against the API ...")
        self.run_worker(lambda: self._check(key), thread=True)

    def _check(self, key: str) -> None:
        error = openai_api.check_key(self.app.cfg.api_base, key)
        self.app.call_from_thread(self._done, key, error)

    def _done(self, key: str, error: str | None) -> None:
        if error:
            self.query_one("#setup-msg", Static).update(error)
            return
        path = config.save_api_key(key)
        self.app.cfg = config.load()
        self.app.pop_screen()
        self.app.flash(f"key saved to {path}")


class MainScreen(Screen):
    """Status, jot box, and the list of meeting notes."""

    BINDINGS = [
        Binding("enter", "open_note", "read", show=True),
        Binding("e", "open_editor", "editor"),
        Binding("n", "jot", "jot"),
        Binding("a", "ask", "ask"),
        Binding("r", "record", "record/stop"),
        Binding("d", "delete_note", "delete"),
        Binding("p", "reprocess", "reprocess", show=False),
        Binding("q", "app.quit", "quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Static(ART, id="art"),
            Vertical(
                Static("razorbill", id="wordmark"),
                Static(TAGLINE, id="tagline"),
                Static("", id="status"),
                Static("", id="flash"),
                id="masthead",
            ),
            id="header",
        )
        yield Input(placeholder="jot a note into the meeting (enter to save)", id="jot")
        yield Static("", id="insight")
        yield Static("live transcript", id="live-section")
        yield VerticalScroll(Static("", id="live-text"), id="live")
        yield Static("meetings", id="section")
        yield ListView(id="notes")
        yield Static("", id="empty")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#jot").display = False
        self.query_one("#live-section").display = False
        self.query_one("#live").display = False
        self._dir_stamp = 0.0
        self._live_stamp = ("", "")
        self._delete_pending: tuple[Path | None, float] = (None, 0.0)
        self._refresh_status()
        self._refresh_notes()
        self.set_interval(1.0, self._refresh_status)
        self.set_interval(0.5, self._refresh_live)
        self.set_interval(3.0, self._maybe_refresh_notes)
        self.query_one("#notes", ListView).focus()

    # --- live status -----------------------------------------------------

    def _refresh_status(self) -> None:
        css, glyph, text = _status()
        w = self.query_one("#status", Static)
        w.set_classes(css)
        w.update(f"{glyph} {text}")
        self.query_one("#jot").display = css == "recording"
        self._refresh_insight(css)

    def _refresh_live(self) -> None:
        """Rolling captions while recording: recent utterances from live.md
        plus the words currently being spoken (streaming interims)."""
        s = state.read_status()
        recording = s.get("state") == "recording"
        lines: list[str] = []
        if recording:
            try:
                raw = (Path(s["dir"]) / meeting.LIVE_MD).read_text()
                for m in re.finditer(r"\*\*\[(.+?)\]\*\*\s*(.+)", raw):
                    lines.append(f"[#6f6a5f]{m.group(1)}[/]  {m.group(2)}")
            except OSError:
                pass
        partial = state.read_partial().strip() if recording else ""
        if partial:
            lines.append(f"[#6f6a5f italic]{partial} ...[/]")

        stamp = (lines[-1] if lines else "", str(len(lines)))
        visible = recording and bool(lines)
        self.query_one("#live-section").display = visible
        self.query_one("#live").display = visible
        if visible and stamp != self._live_stamp:
            self._live_stamp = stamp
            self.query_one("#live-text", Static).update("\n".join(lines[-40:]))
            self.query_one("#live", VerticalScroll).scroll_end(animate=False)

    def _refresh_insight(self, css: str) -> None:
        """Show the newest proactive insight while a meeting is recording."""
        w = self.query_one("#insight", Static)
        latest = ""
        if css == "recording":
            s = state.read_status()
            f = Path(s.get("dir", "")) / meeting.INSIGHTS_MD
            try:
                blocks = [b.strip() for b in f.read_text().split("\n\n") if b.strip()]
                latest = blocks[-1] if blocks else ""
            except OSError:
                pass
        w.display = bool(latest)
        if latest:
            w.update(latest)

    def _maybe_refresh_notes(self) -> None:
        root = self.app.cfg.out_dir()
        try:
            stamp = root.stat().st_mtime
        except OSError:
            stamp = 0.0
        if stamp != self._dir_stamp:
            self._refresh_notes()

    def _refresh_notes(self) -> None:
        root = self.app.cfg.out_dir()
        try:
            self._dir_stamp = root.stat().st_mtime
        except OSError:
            self._dir_stamp = 0.0
        notes = meeting.list_notes(self.app.cfg)
        lv = self.query_one("#notes", ListView)
        prev = lv.highlighted_child.path if isinstance(lv.highlighted_child, NoteItem) else None
        lv.clear()
        for n in notes:
            lv.append(NoteItem(n))
        if notes:
            lv.index = next((i for i, n in enumerate(notes) if n["path"] == prev), 0)
        empty = self.query_one("#empty", Static)
        empty.display = not notes
        if not notes:
            hint = ("recording starts when a meeting app opens the microphone, or press r"
                    if audio.detection_supported()
                    else "press r to start a recording")
            empty.update(f"no notes yet · {hint}")

    # --- actions -----------------------------------------------------------

    def _selected(self) -> Path | None:
        item = self.query_one("#notes", ListView).highlighted_child
        return item.path if isinstance(item, NoteItem) else None

    def action_open_note(self) -> None:
        if path := self._selected():
            self.app.push_screen(NoteScreen(path))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, NoteItem):
            self.app.push_screen(NoteScreen(event.item.path))

    def action_open_editor(self) -> None:
        if path := self._selected():
            state.open_path(path)
            self.app.flash(f"opened {path.name}")

    def action_jot(self) -> None:
        if state.read_status().get("state") != "recording":
            self.app.flash("no meeting is being recorded", "warn")
            return
        self.query_one("#jot", Input).focus()

    def action_ask(self) -> None:
        self.app.push_screen(AskScreen())

    def action_delete_note(self) -> None:
        path = self._selected()
        if path is None:
            return
        pending, asked = self._delete_pending
        if pending == path and time.monotonic() - asked < 4.0:
            self._delete_pending = (None, 0.0)
            try:
                dest = meeting.delete_note(self.app.cfg, path)
            except OSError as e:
                self.app.flash(f"could not delete: {e}", "warn")
                return
            self._refresh_notes()
            self.app.flash(f"moved to {dest.parent.name}/{dest.name}")
        else:
            self._delete_pending = (path, time.monotonic())
            self.app.flash(f"delete '{path.stem}'? press d again", "warn")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        self.query_one("#notes", ListView).focus()
        if text and state.add_jot(text):
            self.app.flash("noted")
        elif text:
            self.app.flash("the meeting ended before the jot landed", "warn")

    def action_record(self) -> None:
        s = state.read_status().get("state")
        if s == "off":
            self.app.flash("the daemon isn't running", "warn")
        elif s == "recording":
            state.request_stop()
            self.app.flash("stopping")
        else:
            state.request_start()
            self.app.flash("starting")

    def action_reprocess(self) -> None:
        pending = meeting.pending(self.app.cfg)
        if not pending:
            self.app.flash("nothing pending")
            return
        self.app.flash(f"reprocessing {len(pending)} meeting(s)")
        self.run_worker(lambda: self._reprocess(pending), thread=True)

    def _reprocess(self, dirs: list[Path]) -> None:
        try:
            api = openai_api.resolve(self.app.cfg)
        except openai_api.ApiError as e:
            self.app.call_from_thread(self.app.flash, str(e), "warn")
            return
        for d in dirs:
            try:
                out = meeting.process(self.app.cfg, api, d)
                self.app.call_from_thread(self.app.flash, f"notes written: {out.name}")
            except Exception as e:
                self.app.call_from_thread(self.app.flash, f"{d.name}: {e}", "warn")


class RazorbillApp(App):
    """razorbill: meeting transcription and notes."""

    TITLE = "razorbill"

    CSS = """
    Screen {
        background: #14151a;
        color: #e9e5dc;
    }
    #header {
        height: 13;
        padding: 1 2 0 2;
    }
    #art {
        width: 27;
        color: #e9e5dc;
    }
    #masthead {
        padding: 0 0 0 2;
    }
    #wordmark {
        text-style: bold;
        color: #e9e5dc;
    }
    #tagline {
        color: #8a857a;
    }
    #flash {
        height: 1;
        margin-top: 1;
        color: #8a857a;
        text-style: italic;
    }
    #flash.flash-warn { color: #d9a24c; }
    NoteScreen #flash { margin: 0 3; }
    #status {
        margin-top: 1;
        color: #8a857a;
    }
    #status.recording { color: #e05d4b; text-style: bold; }
    #status.processing { color: #d9a24c; }
    #status.idle { color: #8a857a; }
    #status.off { color: #5c584f; }
    #jot {
        margin: 0 2;
        border: tall #33363e;
        background: #1b1d23;
    }
    #jot:focus { border: tall #e05d4b; }
    #insight {
        margin: 0 2 1 2;
        padding: 0 1;
        height: auto;
        color: #d9a24c;
        border-left: thick #d9a24c;
    }
    #live-section {
        margin: 0 2;
        color: #8a857a;
        text-style: bold;
    }
    #live {
        height: 9;
        margin: 0 2 1 2;
        padding: 0 1;
        border-left: thick #33363e;
        color: #c9c4b8;
    }
    #ask-input {
        margin: 1 2 0 2;
        border: tall #33363e;
        background: #1b1d23;
    }
    #ask-input:focus { border: tall #e05d4b; }
    #ask-scroll { padding: 1 3; }
    #section {
        margin: 1 2 0 2;
        color: #5c584f;
        text-style: bold;
    }
    #notes {
        margin: 0 1;
        background: #14151a;
        scrollbar-color: #33363e #14151a;
    }
    #empty {
        margin: 1 2;
        color: #5c584f;
    }
    NoteItem {
        padding: 0 1;
        height: 3;
        background: #14151a;
    }
    NoteItem .note-title { text-style: bold; }
    NoteItem .note-meta { color: #8a857a; }
    #notes:focus { background-tint: #e9e5dc 0%; }
    ListView > ListItem.-highlight { background: #22242b; }
    ListView:focus > ListItem.-highlight { background: #2a2d36; }
    ListView:focus > ListItem.-highlight .note-title { color: #d9a24c; }
    #note-scroll {
        padding: 1 3;
        scrollbar-color: #33363e #14151a;
        scrollbar-color-hover: #4a4e58 #14151a;
    }
    Markdown { background: #14151a; }
    MarkdownH1 { color: #e9e5dc; background: transparent; }
    MarkdownH2 { color: #d9a24c; background: transparent; }
    MarkdownH3 { color: #d9a24c; background: transparent; }
    MarkdownBullet { color: #8a857a; }
    #setup-box {
        margin: 2 4;
        padding: 1 2;
        border: round #33363e;
        height: auto;
    }
    #setup-art { color: #e9e5dc; }
    #setup-blurb { margin: 1 0; color: #8a857a; }
    #key-input {
        border: tall #33363e;
        background: #1b1d23;
    }
    #key-input:focus { border: tall #e05d4b; }
    #setup-msg { margin-top: 1; color: #d9a24c; }
    Footer {
        background: #1b1d23;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.cfg = config.load()
        self._flash_timer = None

    def on_mount(self) -> None:
        self.push_screen(MainScreen())
        if not self.cfg.resolve_key():
            self.push_screen(SetupScreen())

    def flash(self, text: str, style: str = "info") -> None:
        """One quiet line of feedback on the current screen. Replaces toasts."""
        try:
            w = self.screen.query_one("#flash", Static)
        except Exception:
            return
        w.set_classes(f"flash-{style}")
        w.update(text)
        if self._flash_timer is not None:
            self._flash_timer.stop()
        self._flash_timer = self.set_timer(3.0, lambda: self._clear_flash(w))

    def _clear_flash(self, w: Static) -> None:
        self._flash_timer = None
        try:
            w.update("")
        except Exception:
            pass


def run() -> None:
    RazorbillApp().run()
