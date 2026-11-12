# Dropdowns, scrolling lists and screenshots

## Contents
- How clicks find their target
- Dropdowns, selects and expandable lists
- Scrolling
- Screenshots: when and how
- When the user takes the mouse

## How clicks find their target

Right before every click, DeskPilot checks where the element is **now**, not where the last snapshot saw it:

- `b` refs (DeskPilot browser): the page reports the visible part of the element, scrolls it into view inside its
  own list or dropdown if needed, waits until it stops moving, and checks nothing covers it.
- `e` refs (desktop apps, the everyday browser): it checks what is under the old point; if the element moved
  (the page scrolled, a list expanded) it finds it again and, if needed, asks the app to scroll it into view.
- If something covers the element (a popup, menu or sticky header) the step fails with
  `… is hidden behind "…"` instead of clicking the wrong thing. Close or scroll it, or take a screenshot.

## Dropdowns, selects and expandable lists

1. Click the dropdown in its own `act` call and read the diff: the options appear as `+` lines.
2. Click the option by its new ref (or text) in the **next** call. Don't batch "open" and "pick" with refs from
   before the list opened: those options had no refs yet.
3. A long list scrolls inside itself. Clicking an option ref scrolls the list to it; you don't need to scroll first.
4. Native `<select>` boxes: click it, then type the option's first letters and press `enter`, or use
   `{"key":"down","repeat":n}` + `enter`. This avoids the popup entirely.
5. If the options don't show up in the diff, or the list opened somewhere unexpected: `screenshot()` and click
   the option you see by `[x, y]`.

## Scrolling

- `{"scroll":"down","amount":5}` scrolls the top window; add `"at": <ref>` to scroll a specific list or panel
  (the mouse must be over it).
- After scrolling, use the refs from the returned diff. Old refs still work (they are re-located), but new ones
  are cheaper.
- On a web page `snapshot()` shows `scroll Y/H`; `(N more off-screen…)` means there is more to reach.

## Screenshots: when and how

Take one instead of guessing whenever the text view isn't enough:
- you can't find or read an element, or `find` keeps returning the wrong thing;
- a click hit the wrong thing or nothing changed;
- dropdowns, menus, pop-overs, drag-and-drop, canvas, charts, maps, games, image-heavy apps;
- before stopping for the user to confirm, to check the form looks right.

Cost, cheapest first:
- `screenshot(ref="e12")` / `screenshot(ref="b12")`: a sharp crop around one element.
- `screenshot(ref="window")` or `screenshot(ref="w3")`: one window.
- `screenshot()`: the whole controlled screen (~1-2k tokens).
- `screenshot(region=[x, y, w, h])`: zoom into part of the previous screenshot (its pixels).
- `marks=true` draws the refs from the last snapshot onto the image, so you can match what you see to refs.

Click what you see with `act([{"click": [x, y]}])`, using pixels of the **last** screenshot (crops included).

## When the user takes the mouse

- DeskPilot never fights the user. If they move the mouse while you work, it steps aside, the border shows
  "You have the mouse", and it continues once the mouse is still and no button is held (about 1.5 s).
- The result then starts with `note: the user took the mouse during step N…`. They may have changed something:
  read the diff, or take a screenshot, before continuing.
- If they keep using it for a minute the call returns `the user has been using the mouse for 60s`. Don't retry in
  a loop. Tell the user you'll continue when they're done, or call `act` again later with the remaining steps.
- A drag interrupted by the user is not redone automatically (the item may already be dropped). Look first.
