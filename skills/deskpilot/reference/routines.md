# Routines

A routine is a saved list of `act` steps that replays with one call. It is the cheapest way to repeat work.

## Use one

1. `routine("list")` at the start of a repeatable task.
2. If one fits: `routine("run", "new app", params={"app_name": "Notes Pro"})`.
3. Check the result as with any `act`.

## Record one

Either record what you do:

1. `routine("start")`
2. Do the task with `act` and `launch` calls.
3. `routine("save", "upload release", description="Upload an APK to the internal testing track")`

Or save explicit steps:

```
routine("save", "new app", steps=[
  {"launch": "https://play.google.com/console"},
  {"wait": "All apps", "timeout": 20},
  {"click": "Create app"},
  {"type": "{{app_name}}", "into": "App name"}
], description="Start creating a Play Console app")
```

## Rules

- Write `{{placeholders}}` for values that change between runs.
- Target visible text, not refs: refs don't survive between sessions. Recording converts refs to text for you.
- Add a `wait` after each navigation so replays don't race the page.
- Keep outward actions (submit, publish) out of routines, or end the routine just before them.
- Manage routines with `routine("show", name)` and `routine("delete", name)`. They are stored in
  `~/.deskpilot/routines/`.
