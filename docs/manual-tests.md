# Manual tests

CI runs the unit tests on Windows, but some things need a real desktop and a person: UAC prompts, admin windows,
the kill switch. Run this list before a release. Use a normal (not elevated) Claude Code session with DeskPilot
installed from your working copy:

```powershell
uv tool install --reinstall --python 3.13 .
```

Stop running DeskPilot servers first (close the AI sessions using it), or the install can't replace the exe.

## Admin windows (UIPI)

Open an app that runs as administrator: Task Manager (`taskmgr`), Registry Editor (`regedit`) or any installer.
Approve its UAC prompt yourself.

| # | Ask the agent / do | Expected |
|---|---|---|
| 1 | `window("list")` | The admin app's line ends in `admin` |
| 2 | `snapshot()` with the admin app on top | Starts with `(ADMIN WINDOW: …)`, and few or no elements |
| 3 | Click a button in it | `ADMIN WINDOW: … runs as administrator …`, the step is **not** run, and nothing behind it is clicked |
| 4 | Type into it, or press a key | Same message, and no keys are sent |
| 5 | With the admin app in front, click something in a normal window on your screen | `ADMIN WINDOW IN FRONT: …`, not "the user took the mouse"; works after `window("focus", …)` on the normal window |
| 6 | `screenshot()` and `snapshot(scope="ocr")` | Both read the admin window |
| 7 | `screen_info(op="admin")`, then **Deny** in the DeskPilot dialog | `USER DENIED`; no UAC prompt |
| 8 | `screen_info(op="admin")`, Allow, then **No** on the UAC prompt | `Admin mode is NOT on: the user declined…` |
| 9 | `screen_info(op="admin")`, Allow, then **Yes** on UAC | `ADMIN MODE ON`; `screen_info()` shows `admin mode: ON` |
| 10 | `snapshot()` of the admin app | Its buttons and fields are listed |
| 11 | Click and type in it (e.g. Task Manager's search box) | It works: the pointer moves smoothly (no "user took the mouse" notes) and the step report shows the screen changed |
| 11a | Without admin mode, `window("focus", …)` on an admin window that is on another screen | A clear ADMIN WINDOW message (can't move it), not `Access is denied` |
| 12 | Do something in a normal app (Notepad) | Still works in admin mode |
| 13 | `launch("notepad")`, then check Task Manager > Details > Elevated | Notepad is **not** elevated |
| 14 | Wait 1 minute without calls, then act again | Admin mode is still on (the overlay hid, admin mode didn't) |
| 15 | `done()` | Ends with `Admin mode is off again`; a click in the admin app is blocked again |
| 16 | Turn admin mode on, then wait more than 5 minutes | It turns off; `audit.jsonl` has `"reason": "idle"` |
| 17 | Turn admin mode on, hold `{"key_down": "shift"}` in the admin app, then the kill switch (Ctrl+Alt+Backspace) | DeskPilot ends, the helper process is gone (Task Manager), and Shift isn't stuck |
| 18 | `admin_mode: always` in the policy, restart the agent, run any tool | One DeskPilot UAC prompt at the first call; admin mode stays on after `done()` |
| 19 | `admin_mode: off` in the policy | `screen_info(op="admin")` refuses; messages don't suggest admin mode |

## Secure desktop (UAC prompt)

| # | Ask the agent / do | Expected |
|---|---|---|
| 20 | `launch("regedit")` and leave its UAC prompt open | The result, or the next `snapshot()`/`screenshot()`, says `WINDOWS SECURE DESKTOP` instead of only "all black" |
| 21 | `act(...)` while the prompt is open | `WINDOWS SECURE DESKTOP … None of your steps were run.` |
| 22 | Answer the prompt, then `snapshot()` | Normal again |

## Launching admin apps and stray clicks

| # | Ask the agent / do | Expected |
|---|---|---|
| 22a | `launch("Registry Editor")` (or HWiNFO, GPU-Z, msconfig) | The result waits while the UAC prompt is up, then names the window and says it runs as administrator |
| 22b | Same, but answer **No** on UAC | `NOTE: … asks for administrator rights … No window yet: the user may have said No` |
| 22c | `launch("notepad")` | No admin note |
| 22d | In Explorer, double-click a .txt file | Step report: `⚠ "… Notepad" (Notepad.exe) is now in front, not the window clicked ("…")` |
| 22e | Click a button in Notepad (e.g. a menu) | No `is now in front` note |

## Regression spot checks

| # | Ask the agent / do | Expected |
|---|---|---|
| 23 | "Open Notepad and type hello" | Works, with the overlay, panel and step report |
| 24 | Ctrl+Alt+Esc during an act | Pauses; resumes from the panel |
| 25 | `done()` | Overlay and panel disappear; steps listed |
