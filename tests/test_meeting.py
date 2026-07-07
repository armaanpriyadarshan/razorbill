from razorbill.meeting import _slug, read_frontmatter, strip_frontmatter

NOTE = """---
title: "Weekly Standup Sync"
date: 2026-07-06 15:29
duration_minutes: 31
app: "Firefox"
---

# Weekly Standup Sync

body here
"""


def test_slug():
    assert _slug("Weekly Standup: Q3 Planning!") == "weekly-standup-q3-planning"
    assert _slug("???") == "meeting"
    assert len(_slug("x" * 200)) <= 48


def test_read_frontmatter():
    meta = read_frontmatter(NOTE)
    assert meta["title"] == "Weekly Standup Sync"
    assert meta["duration_minutes"] == "31"
    assert meta["app"] == "Firefox"


def test_strip_frontmatter():
    body = strip_frontmatter(NOTE)
    assert body.startswith("# Weekly Standup Sync")
    assert "duration_minutes" not in body


def test_strip_frontmatter_without_frontmatter():
    assert strip_frontmatter("# Title\n\nplain") == "# Title\n\nplain"
