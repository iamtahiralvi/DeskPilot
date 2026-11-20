# act() steps

`act(steps, observe="diff")` runs the steps in order with the real mouse and keyboard, then returns what changed.
`observe`: `diff` (default), `full` (whole snapshot), `screenshot` (diff plus an image of the screen: act and look
in one call, best for canvas apps), `none` (fastest, when you'll check yourself).
If a step fails, the rest are skipped and the result says which step failed and why.

The result has one line per step, for example:

```
ok (3 steps)
1 click "e12" → screen changed; focus edit "Height"
2 type "200" → verified (OCR); screen changed; focus edit "Height"
3 paste file body.dart → pasted 10234 chars from body.dart; your clipboard was put back; screen changed
```

`NO visible change` on a step means it probably did nothing (focus was elsewhere, the app was still loading).
If no step changed the screen, the first line is a `⚠` warning instead of `ok`. Don't carry on as if it worked.

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
| OCR text | `{"ocr": "Save Widget"}`, `{"ocr": "Save", "nth": 2, "in": "window"}` | Text read off the screen image. For canvas apps (Flutter web apps, design tools) where `find` sees nothing |
| pixels | `[640, 360]` | Coordinates in the last screenshot's image. After a cropped screenshot (`region` or `ref`), the crop's pixels |

A region `R` (for `read`, `wait_stable`, `snapshot(region=…)`, OCR `in`) is `null` (your screen), `"window"`, a ref,
`{"ocr": "Height"}` (around that text), or `[x, y, w, h]` in the last screenshot's pixels.

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
| type, checked | `{"type": "200", "into": [812, 344], "clear": true, "verify": true}`, `"delay_ms": 60` |
| paste text | `{"paste": "…10 KB of code…", "into": "e5", "clear": true}` |
| paste a file | `{"paste_file": "C:\\work\\body.dart", "into": "e5", "clear": true}` |
| go to a URL | `{"navigate": "https://console.firebase.google.com"}`, `"new_tab": true`, `"in": "w3"` |
| key combo | `{"key": "ctrl+shift+n"}`, `{"key": "tab", "repeat": 3}` |
| hold / release | `{"key_down": "shift"}` … `{"key_up": "shift"}` |

Key names: `enter`, `tab`, `esc`, `space`, `backspace`, `delete`, `home`, `end`, `pageup`, `pagedown`,
`up/down/left/right`, `f1`…`f24`, `win`, `ctrl`, `alt`, `shift`.

- `type` sends real keystrokes (about 12 ms per character) and **never touches the clipboard**.
- `verify: true` reads the field back (its value, or OCR of the field when the app has no accessible value) and
  retypes more slowly if keys were dropped. Canvas apps that redraw on every key can turn "200" into "2". Use it for
  short values in such apps; `delay_ms` sets a slower speed from the start.
- `paste` / `paste_file` are for long text. They save the user's clipboard, paste, and put it back. The text is also
  kept out of Win+V history. The report says how many characters were pasted. Don't use `clipboard("set")` +
  Ctrl+V for this: that loses whatever the user had copied.
- `navigate` focuses the browser window and its address bar (checking that the cursor is really there), types the
  URL, and presses Enter. It doesn't touch the clipboard, and the report shows the address bar afterwards.

## Waiting

| Step | Example |
|---|---|
| wait for text | `{"wait": "Inbox", "timeout": 15}` (also counts text scrolled out of view) |
| wait until gone | `{"wait": "Uploading", "gone": true, "timeout": 90}` |
| wait for text by OCR | `{"wait_ocr": "Save", "timeout": 60}` (apps with no accessible text; `"gone": true`, `"in": R`) |
| wait until still | `{"wait_stable": "window", "timeout": 60, "quiet": 1}` (until that area stops changing) |
| wait for a change, then still | `{"wait_stable": null, "after_change": true, "timeout": 60}` |
| read text | `{"read": {"ocr": "Height"}}` or `{"read": [400, 220, 300, 40]}`: the step's line shows the text |
| fixed pause | `{"sleep": 1.5}` or `{"wait": 1.5}` (a number = seconds; max 30; prefer waiting for text) |

A splash screen that shows a static logo is already "still". For those, wait for text that appears afterwards
(`wait_ocr`), or use `after_change`.

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

Replace all code in an editor with a file's contents (the user's clipboard is kept):
```json
[{"paste_file": "C:\\work\\body.dart", "into": "e1", "clear": true}]
```

A canvas app (no readable elements): open a page, wait for it, select something and look, in one round trip:
```json
act([{"navigate": "https://app.example.com/editor"},
     {"wait_ocr": "Layers", "timeout": 60},
     {"click": {"ocr": "Container"}}],
    observe="screenshot")
// then, with the input box's pixels from that screenshot (an OCR target is the label, not the box beside it):
act([{"type": "200", "into": [812, 344], "clear": true, "verify": true}])
```
