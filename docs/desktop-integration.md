# Desktop integration

None of this is required. razorbill works through the TUI, the CLI, and
(where available) desktop notifications. These recipes surface daemon state
in bars; the building blocks are `statusline` (one line of state),
`toggle`, and `last`.

On Linux, notifications carry actions: Stop on the recording notification,
Open on the notes-ready one. Notification daemons map these to a click or
menu (dunst: middle-click).

Polybar:

```ini
[module/razorbill]
type = custom/script
exec = ~/.local/bin/razorbill statusline --polybar
interval = 2
click-left = ~/.local/bin/razorbill toggle
click-right = ~/.local/bin/razorbill last
```

Waybar:

```json
"custom/razorbill": {
    "exec": "~/.local/bin/razorbill statusline",
    "interval": 2,
    "on-click": "~/.local/bin/razorbill toggle",
    "on-click-right": "~/.local/bin/razorbill last"
}
```

i3, a record toggle hotkey:

```
bindsym $mod+r exec --no-startup-id ~/.local/bin/razorbill toggle
```

Running the daemon as a service: on Linux, copy `razorbill.service` to
`~/.config/systemd/user/` and `systemctl --user enable --now razorbill`.
On macOS, a launchd agent running `razorbill run` does the same job; on
Windows, Task Scheduler.
