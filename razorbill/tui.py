"""The razorbill terminal interface.

A thin client over the same files the daemon and CLI use: status comes from
$XDG_RUNTIME_DIR/razorbill/status.json, actions are marker files, notes are
Markdown on disk. The TUI runs anywhere a terminal runs, including machines
where the recording daemon itself can't (see README, platform support).
"""

from __future__ import annotations

import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Input, Label, ListItem, ListView, Markdown, Static

from . import config, meeting, openai_api, state

# A razorbill looking down its own bill, drawn from a portrait photo: the
# rounded head, the white line arcing from bill base to eye, the stripe
# crossing near the bill tip, and the white breast below the shoulder.
ART = "\n".join(
    (
        "    .--~~--.",
        "   /        \\",
        "  |  ,__.--o |",
        "   \\ \\        \\",
        "    \\=\\        \\__",
        "     `-\\     __.-'",
        "        `)  (",
    )
)

TAGLINE = "meeting notes that write themselves"


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
        yield Footer()

    def action_open_editor(self) -> None:
        state.open_path(self.path)
        self.notify(f"opened {self.path.name}")


class SetupScreen(Screen):
    """First run: collect and verify the OpenAI API key."""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(ART, id="setup-art"),
            Static("razorbill needs an OpenAI API key to transcribe meetings\n"
                   "and write notes. It is stored in ~/.config/razorbill/config.toml\n"
                   "with owner-only permissions, and used for nothing else.", id="setup-blurb"),
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
        self.notify(f"key saved to {path}")
        self.app.pop_screen()


class MainScreen(Screen):
    """Status, jot box, and the list of meeting notes."""

    BINDINGS = [
        Binding("enter", "open_note", "read", show=True),
        Binding("e", "open_editor", "editor"),
        Binding("n", "jot", "jot"),
        Binding("r", "record", "record/stop"),
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
                id="masthead",
            ),
            id="header",
        )
        yield Input(placeholder="jot a note into the meeting (enter to save)", id="jot")
        yield Static("meetings", id="section")
        yield ListView(id="notes")
        yield Static("", id="empty")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#jot").display = False
        self._dir_stamp = 0.0
        self._refresh_status()
        self._refresh_notes()
        self.set_interval(1.0, self._refresh_status)
        self.set_interval(3.0, self._maybe_refresh_notes)
        self.query_one("#notes", ListView).focus()

    # --- live status -----------------------------------------------------

    def _refresh_status(self) -> None:
        css, glyph, text = _status()
        w = self.query_one("#status", Static)
        w.set_classes(css)
        w.update(f"{glyph} {text}")
        self.query_one("#jot").display = css == "recording"

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
            empty.update("no meetings yet · join a call and razorbill will pick it up,\n"
                         "or press r to record right now")

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
            self.notify(f"opened {path.name}")

    def action_jot(self) -> None:
        if state.read_status().get("state") != "recording":
            self.notify("no meeting is being recorded", severity="warning")
            return
        self.query_one("#jot", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        self.query_one("#notes", ListView).focus()
        if text and state.add_jot(text):
            self.notify("noted")
        elif text:
            self.notify("the meeting ended before the jot landed", severity="warning")

    def action_record(self) -> None:
        s = state.read_status().get("state")
        if s == "off":
            self.notify("the daemon isn't running", severity="warning")
        elif s == "recording":
            state.request_stop()
            self.notify("stopping · notes on the way")
        else:
            state.request_start()
            self.notify("recording starts in a moment")

    def action_reprocess(self) -> None:
        pending = meeting.pending(self.app.cfg)
        if not pending:
            self.notify("nothing pending")
            return
        self.notify(f"reprocessing {len(pending)} meeting(s) in the background")
        self.run_worker(lambda: self._reprocess(pending), thread=True)

    def _reprocess(self, dirs: list[Path]) -> None:
        try:
            api = openai_api.resolve(self.app.cfg)
        except openai_api.ApiError as e:
            self.app.call_from_thread(self.notify, str(e), severity="error")
            return
        for d in dirs:
            try:
                out = meeting.process(self.app.cfg, api, d)
                self.app.call_from_thread(self.notify, f"notes written: {out.name}")
            except Exception as e:
                self.app.call_from_thread(self.notify, f"{d.name}: {e}", severity="error")


class RazorbillApp(App):
    """razorbill: meeting notes that write themselves."""

    TITLE = "razorbill"

    CSS = """
    Screen {
        background: #14151a;
        color: #e9e5dc;
    }
    #header {
        height: 9;
        padding: 1 2 0 2;
    }
    #art {
        width: 24;
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
    Toast { background: #23252c; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.cfg = config.load()

    def on_mount(self) -> None:
        self.push_screen(MainScreen())
        if not self.cfg.resolve_key():
            self.push_screen(SetupScreen())


def run() -> None:
    RazorbillApp().run()
