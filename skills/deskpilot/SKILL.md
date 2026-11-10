---
name: deskpilot
description: Use the user's Windows computer through the deskpilot MCP tools (snapshot, find, act, launch, window, clipboard, routine, screenshot, done) - open apps and websites (e.g. Google Play Console), click, type, and navigate any GUI like a person, with as few tokens as possible. Use whenever the user asks you to do something on their computer, screen, desktop, browser or in a desktop app.
---

# Using the computer with DeskPilot

You control ONE screen the user chose. It has a thin black-and-white border, and you move the real mouse pointer. The user watches
through a control panel where they can Pause, Take over or Stop you, or press Ctrl+Alt+Esc.

## 0. Look before you open anything
Run `snapshot()` (or `window("list")`) first. A web page shows as `page <url>`. If the page or app you need is
already open, work in it: don't open a second copy, and don't press Ctrl+L to retype the URL you're already on.
`launch(url)` and `launch("App")` switch to an existing tab or window automatically. Pass `new_window=true` only
when you really need another copy.

## 0.5 Check saved routines
Run `routine("list")`. If one matches the task, use `routine("run", name, params={...})`. That's the
cheapest path by far.

## 1. The cheap loop
1. **Look with text, not pixels.** Use `find(text="Create app")` when you know what you want; otherwise
   `snapshot()` or `snapshot(filter="...")`.
   - Desktop apps give `e12 button "Install"`.
   - Web pages in Chrome/Edge give `b12 button "Create app"`. These are precise, and off-screen elements
     are reachable (they are scrolled into view automatically when clicked).
   - `snapshot(scope="page")` lists the whole web page.
2. **Act in batches.** Put every step you're confident about in ONE `act` call:
   `act([{"click":"Create app"},{"type":"My App","into":"App name"},{"click":"Save draft"}])`
   - Targets: a ref (`e12`/`b12`), visible text (`"Sign in"`), or `[x,y]` from the last screenshot.
3. **Read the diff** that `act` returns (`+` new elements, `-` removed). Re-snapshot only if it's unclear.
4. **Escalate only when needed.** First `snapshot(scope="ocr")`, then `screenshot(ref="e5")` (a small
   crop), then a full `screenshot()` (~1k+ tokens).

## 2. Websites (Play Console, dashboards, etc.)
- Work in the browser that is already open on your screen. To go to a site, prefer `launch("https://…")`: it reuses
  an open tab for that page. Navigate inside a tab with
  `{"key":"ctrl+l"},{"type":"play.google.com/console","enter":true},{"wait":"All apps","timeout":20}` only when
  the current `page` URL is different.
- The user's everyday browser is read through the screen (`e` refs). That is normal and works for most pages.
  If you need precise control (`b` refs, off-screen elements), `launch("agent browser https://…")` opens the
  DeskPilot browser, where the user signs in once.
- If a window called "Allow remote debugging?" appears, never click it yourself. It is the user's decision.

## 3. Save repeatable work as routines
When the user will repeat a task:
1. `routine("start")`.
2. Do the task.
3. `routine("save", "upload release", description="...")`.

Or save explicit steps with `{{placeholders}}`:
`routine("save","new app",steps=[{"click":"Create app"},{"type":"{{app_name}}","into":"App name"}])`.
Prefer visible-text targets in routines, because refs don't survive between sessions.

## 4. Tips
- `launch`: app names ("Microsoft Store", "Notepad"), URLs, `ms-settings:`, or file paths. New windows
  move onto your screen.
- Use `{"wait":"text"}` instead of sleeping and re-snapshotting.
- For long text, use `clipboard("set", text)` then `{"key":"ctrl+v"}`.
- To replace a field's content, use `"clear": true`.
- If a window is on another screen: `window("list")`, then `window("move_here","w3")`.

## 5. Permissions and privacy
- Sensitive steps open a confirm dialog for the user. These include:
  - delete, pay, buy, send, install, uninstall, sign out
  - Play Console: rollout, release to production, unpublish, send for review
  - closing windows
  - password fields
  - terminals

  On **USER DENIED**, don't retry or work around it. Tell the user and ask.
- On **PAUSED** or **STOPPED**, stop at once and tell the user what's done and what's left.

## 6. Finish
When the task is complete, or you are stopping for any reason, call `done(summary="...")` as your last
DeskPilot call. It removes the border, your cursor and the control panel so the user gets their screen back.
- `[secret]`, `[card]`, `***` and "private window" mean the data is hidden on purpose. Never try to
  reveal it.
