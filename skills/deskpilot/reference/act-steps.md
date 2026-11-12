# act() steps

`act(steps, observe="diff")` runs the steps in order with the real mouse and keyboard, then returns what changed.
`observe`: `diff` (default), `full` (whole snapshot), `none` (fastest, when you'll check yourself).
If a step fails, the rest are skipped and the result says which step failed and why.

## Contents
- Targets
- Mouse
- Keyboard
- Waiting
- Windows and apps
- Examples

## Targets

| Form | Example | Notes |
|---|---|---|
| ref | `"e12"`, `"b31"` | From the latest snapshot/find. Re-find after the page changes |
| visible text | `"Sign in"` | First visible match on your screen |
| pixels | `[640, 360]` | Coordinates in the last screenshot's image |

## Mouse

| Step | Example |
|---|---|
| click | `{"click": "Save"}` |
| double-click | `{"dblclick": "e7"}` |
| right-click | `{"rclick": "report.pdf"}` |
| hover | `{"hover": "Settings", "hold": 0.8}` |
| scroll | `{"scroll": "down", "amount": 5, "at": "e3"}` (`up`, `left`, `right`; `at` optional) |
| drag | `{"drag": "e4", "to": "e9"}` |

## Keyboard

| Step | Example |
|---|---|
| type | `{"type": "hello"}`, `{"type": "a@b.com", "into": "Email", "clear": true, "enter": true}` |
| key combo | `{"key": "ctrl+shift+n"}`, `{"key": "tab", "repeat": 3}` |
| hold / release | `{"key_down": "shift"}` … `{"key_up": "shift"}` |

Key names: `enter`, `tab`, `esc`, `space`, `backspace`, `delete`, `home`, `end`, `pageup`, `pagedown`,
`up/down/left/right`, `f1`…`f24`, `win`, `ctrl`, `alt`, `shift`.

Typing is about 12 ms per character. For text over ~200 characters, use `clipboard("set", …)` + `{"key":"ctrl+v"}`.

## Waiting

| Step | Example |
|---|---|
| wait for text | `{"wait": "Inbox", "timeout": 15}` (also counts text scrolled out of view) |
| wait until gone | `{"wait": "Uploading", "gone": true, "timeout": 90}` |
| fixed pause | `{"sleep": 1.5}` or `{"wait": 1.5}` (a number = seconds; max 30; prefer waiting for text) |

## Windows and apps

| Step | Example |
|---|---|
| focus a window | `{"focus": "w3"}` or `{"focus": "Notepad"}` |
| launch / switch | `{"launch": "Calculator"}`; force a new copy: `{"launch": "https://x.com", "new": true}` |

## Examples

Fill a form and stop before submitting:
```json
[{"type": "Tahir", "into": "First name", "clear": true},
 {"type": "tahir@example.com", "into": "Email", "clear": true},
 {"click": "I agree"}]
```

Search inside a page and open the first result:
```json
[{"click": "Search"}, {"type": "invoice 2026", "enter": true}, {"wait": "results", "timeout": 10}]
```

Select all text in an editor and replace it:
```json
[{"click": "e1"}, {"key": "ctrl+a"}, {"key": "ctrl+v"}]
```
