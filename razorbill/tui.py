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

from rich.text import Text
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
    """Status, live captions, and the list of meeting notes."""

    BINDINGS = [
        Binding("enter", "open_note", "read", show=True),
        Binding("e", "open_editor", "editor"),
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
        yield Static("live transcript", id="live-section")
        yield VerticalScroll(id="live")
        yield Static("", id="live-partial")
        yield Static("meetings", id="section")
        yield ListView(id="notes")
        yield Static("", id="empty")
        yield Footer()

    def on_mount(self) -> None:
        for wid in ("#live-section", "#live", "#live-partial"):
            self.query_one(wid).display = False
        self._dir_stamp = 0.0
        self._live_dir: Path | None = None
        self._live_count = 0
        self._partial_shown = ""
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

    @staticmethod
    def _caption(stamp: str, label: str | None, text: str) -> Text:
        """One transcript line as a Rich renderable. Text objects, never
        markup strings: transcript content can contain anything, including
        square brackets that markup would try to parse."""
        out = Text(no_wrap=False)
        out.append(stamp, style="#6f6a5f")
        out.append("  ")
        if label:
            color = "#e05d4b" if label.startswith("Me") else "#d9a24c"
            out.append(label + " ", style=f"bold {color}")
        out.append(text, style="#c9c4b8")
        return out

    async def _refresh_live(self) -> None:
        """Rolling captions while recording. Finalized utterances are
        appended once and never re-rendered; the in-progress sentence is one
        widget updated in place. Auto-scroll only when already at the
        bottom, so reading back is never interrupted."""
        s = state.read_status()
        recording = s.get("state") == "recording"
        sc = self.query_one("#live", VerticalScroll)
        partial_w = self.query_one("#live-partial", Static)

        if not recording:
            if self._live_dir is not None:
                self._live_dir = None
                self._live_count = 0
                self._partial_shown = ""
                await sc.remove_children()
            for wid in ("#live-section", "#live", "#live-partial"):
                self.query_one(wid).display = False
            return

        d = Path(s["dir"])
        if d != self._live_dir:  # a new meeting began
            self._live_dir = d
            self._live_count = 0
            self._partial_shown = ""
            await sc.remove_children()

        try:
            raw = (d / meeting.LIVE_MD).read_text()
        except OSError:
            raw = ""
        matches = re.findall(r"\*\*\[(.+?)\](?:\s+([^*]+?))?\*\*\s*(.+)", raw)
        partial = state.read_partial().strip()

        visible = bool(matches or partial)
        self.query_one("#live-section").display = visible
        sc.display = visible
        partial_w.display = bool(partial)
        if not visible:
            return

        at_bottom = sc.scroll_offset.y >= sc.max_scroll_y - 1
        fresh = matches[self._live_count:]
        if fresh:
            await sc.mount_all(
                Static(self._caption(st, lb, tx), classes="live-line")
                for st, lb, tx in fresh
            )
            self._live_count = len(matches)
            overflow = len(sc.children) - 80
            for w in list(sc.children)[:max(0, overflow)]:
                w.remove()

        if partial != self._partial_shown:
            self._partial_shown = partial
            spoken = Text(no_wrap=False)
            for i, line in enumerate(partial.splitlines()):
                if i:
                    spoken.append("\n")
                spoken.append(line + " ...", style="italic #6f6a5f")
            partial_w.update(spoken)

        if fresh and at_bottom:
            sc.scroll_end(animate=False)

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
    #live-section {
        margin: 0 2;
        color: #8a857a;
        text-style: bold;
    }
    #live {
        height: 9;
        margin: 0 2;
        padding: 0 1;
        border-left: thick #33363e;
        scrollbar-size-vertical: 1;
    }
    .live-line {
        height: auto;
        margin-bottom: 0;
    }
    #live-partial {
        height: auto;
        margin: 0 2 1 2;
        padding: 0 1;
        border-left: thick #23252c;
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
        w.update(Text(text))  # plain Text: messages may contain brackets
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
