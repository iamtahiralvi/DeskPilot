# Troubleshooting

| Result or symptom | Meaning | What to do |
|---|---|---|
| `note: the user took the mouse…` | The user used the mouse; DeskPilot waited for them to let go and redid the step | Continue, but read the diff first: they may have changed the screen |
| `the user has been using the mouse for 60s` | They are still working | Don't fight them. Wait, then call `act` again with the remaining steps |
| `… is hidden behind "…"` | A popup, menu or sticky header covers the element | Close/scroll it, or `screenshot()` and click what you see |
| `PAUSED` / `STOPPED` by the user | The user paused or stopped you | Stop at once and report. Never resume yourself |
| `USER DENIED` | The user refused a permission dialog | Don't retry or work around it. Ask the user |
| `not found: <text>` | No visible element matches | Check spelling, scroll, try a shorter text, `snapshot(scope="ocr")` |
| A click hits the wrong thing | A stale ref or a moved layout | `screenshot()` to see what's there, then click the new ref or `[x, y]` |
| Typing goes to the wrong place | Focus was elsewhere | Click the field first, or use `"into": "<field>"` |
| Ctrl+L does nothing | The page captured the key | Click `Address and search bar` instead |
| `no visible change` | The click didn't do anything visible | Verify with `find`; the site may still be loading: `{"wait": …}` |
| Only `e` refs on a web page | Read through accessibility | Normal. Use the DeskPilot browser for `b` refs if you need precision |
| `outside the screen you are allowed to control` | The target is on another monitor | `window("move_here", "w3")`, or ask the user to `screen_info(op="switch")` |
| `private window` | A password manager or security window | Never interact with it. Ask the user |
| A dialog appears that you didn't expect | A system or site prompt | Read it. If it asks for consent, credentials or payment, stop and ask the user |
| The app has no accessibility tree | Some games and custom-drawn apps | `snapshot(scope="ocr")` gives clickable OCR refs |

## Before reporting a failure

1. `screen_info()`: are you on the right screen? Paused?
2. `snapshot()`: what window is on top, and which page?
3. One targeted retry with a fresh ref. Don't loop. After two failed tries, tell the user what you see.
