"""CLI: tui (default) | run | statusline | toggle | last | status | note | start | stop | reprocess."""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__, config, daemon, meeting, openai_api, state

# polybar format colors
REC_COLOR = "#e06060"
PROC_COLOR = "#d5a35b"
DIM_COLOR = "#777777"


def cmd_run(_args) -> None:
    daemon.run(config.load())


def cmd_tui(_args) -> None:
    from . import tui  # lazy: the daemon never pays the import

    tui.run()


def cmd_status(args) -> None:
    s = state.read_status()
    if getattr(args, "json", False):
        print(json.dumps(s))
        return
    if s.get("state") == "recording":
        mins = (time.time() - s.get("since", time.time())) / 60
        print(f"recording  {s.get('app')}  ({mins:.0f} min)")
    elif s.get("state") == "processing":
        print("writing notes for the last meeting")
    elif s.get("state") == "idle":
        print("idle, waiting for a meeting")
    else:
        print("daemon not running")


def cmd_statusline(args) -> None:
    """One-line status for a bar (polybar custom/script module)."""
    def paint(text: str, color: str) -> str:
        return f"%{{F{color}}}{text}%{{F-}}" if args.polybar else text

    s = state.read_status()
    st = s.get("state")
    if st == "recording":
        mins = int((time.time() - s.get("since", time.time())) / 60)
        print(paint(f"● {s.get('app', '?')} {mins}m", REC_COLOR))
    elif st == "processing":
        print(paint("✎ notes…", PROC_COLOR))
    elif st == "idle":
        print(paint("○", DIM_COLOR))
    else:
        print("")  # daemon off: hide the module


def cmd_toggle(_args) -> None:
    s = state.read_status()
    if s.get("state") == "off":
        raise SystemExit("daemon not running")
    if s.get("state") == "recording":
        state.request_stop()
        print("stopping")
    else:
        state.request_start()
        print("starting")


def cmd_last(_args) -> None:
    cfg = config.load()
    notes = sorted(cfg.out_dir().glob("*.md"))
    if not notes:
        raise SystemExit("no meeting notes yet")
    print(notes[-1])
    state.open_path(notes[-1])


def cmd_note(args) -> None:
    if not state.add_jot(" ".join(args.text)):
        raise SystemExit("no meeting is being recorded")
    print("noted")


def cmd_start(_args) -> None:
    if state.read_status().get("state") == "off":
        raise SystemExit("daemon not running. Start it first: razorbill run")
    state.request_start()
    print("recording will start within a couple of seconds (stop with: razorbill stop)")


def cmd_stop(_args) -> None:
    if not state.request_stop():
        raise SystemExit("no meeting is being recorded")
    print("stopping. Notes will be generated now")


def cmd_bird(_args) -> None:
    from importlib.resources import files

    print(files("razorbill").joinpath("bird.txt").read_text())


def cmd_reprocess(_args) -> None:
    cfg = config.load()
    api = openai_api.resolve(cfg)
    dirs = meeting.pending(cfg)
    if not dirs:
        print("nothing pending")
        return
    for d in dirs:
        print(f"processing {d.name} ...", flush=True)
        try:
            print(f"  -> {meeting.process(cfg, api, d)}")
        except Exception as e:
            print(f"  failed: {e}", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser(
        prog="razorbill",
        description="Meeting transcription and notes from system audio, with your own "
                    "API key. No arguments opens the TUI.",
    )
    p.add_argument("-V", "--version", action="version", version=f"razorbill {__version__}")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("tui", help="open the terminal interface (default)")
    sub.add_parser("run", help="run the recording daemon")
    st = sub.add_parser("status", help="show daemon state")
    st.add_argument("--json", action="store_true", help="machine-readable output")
    sl = sub.add_parser("statusline", help="one-line status for a bar module")
    sl.add_argument("--polybar", action="store_true", help="emit polybar color tags")
    sub.add_parser("toggle", help="start recording if idle, stop if recording")
    sub.add_parser("last", help="print and open the newest meeting note")
    n = sub.add_parser("note", help="jot a note into the current meeting (anchors the AI notes)")
    n.add_argument("text", nargs="+")
    sub.add_parser("start", help="start recording now, without waiting for detection")
    sub.add_parser("stop", help="stop the current recording and generate notes")
    sub.add_parser("reprocess", help="retry failed/unfinished meetings")
    sub.add_parser("bird", help="print the ASCII artwork")

    args = p.parse_args()
    cmd = args.cmd or "tui"
    {
        "tui": cmd_tui,
        "bird": cmd_bird,
        "run": cmd_run,
        "status": cmd_status,
        "statusline": cmd_statusline,
        "toggle": cmd_toggle,
        "last": cmd_last,
        "note": cmd_note,
        "start": cmd_start,
        "stop": cmd_stop,
        "reprocess": cmd_reprocess,
    }[cmd](args)


if __name__ == "__main__":
    main()
