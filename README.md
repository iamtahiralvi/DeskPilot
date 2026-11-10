# DeskPilot

**A low-token Windows computer-use MCP server.** Let Claude, Codex, Cursor, Copilot or any other MCP agent use your real Windows PC the way you do: desktop apps, File Explorer, Settings, and websites in your own browser. You watch everything it does and can stop it at any moment.

[![License: MIT](https://img.shields.io/badge/License-MIT-black.svg)](LICENSE)
![Platform: Windows](https://img.shields.io/badge/platform-Windows%2011-black)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-black)
![MCP](https://img.shields.io/badge/MCP-stdio-black)

> **Platform note:** DeskPilot has been tested on **Windows 11 only**. Windows 10 will probably work but is untested. macOS and Linux are not supported, because it is built on Windows UI Automation, Win32 input and Windows OCR.

---

## Why DeskPilot

Most computer-use tools send a screenshot to the model on every step, which is slow and costs a lot of tokens. DeskPilot reads the screen as **text**:

```
e12 button "Install"
b31 link "Create app"
e40 edit "Search" ="hello"
```

The agent clicks by reference (`e12`) or by visible text (`"Install"`). After each batch of actions it gets back **only what changed**. Screenshots are a last resort.

## Features

- **🖥️ Pick a display.** With more than one monitor, you choose which one the agent may see and control. It can't touch the others. You can also allow all screens.
- **🖱️ One cursor.** The agent drives your real mouse pointer, so there is no second, fake cursor. A small "Claude" tag follows the pointer while the agent acts.
- **🔲 Clear on-screen status.** A thin black-and-white border marks the controlled screen (dashed when paused), with a status chip and click ripples.
- **🎛️ Control panel.** A floating panel shows the current step, recent activity and token use, with **Pause / Take over / Stop** buttons. When you have a second monitor, it sits there so it never covers the agent's work.
- **⏹️ Always interruptible.** **Ctrl+Alt+Esc** pauses or resumes at any time. If you move the mouse while the agent is moving it, the agent pauses.
- **👻 Gets out of your way.** When a task finishes, the border, tag and panel disappear.
- **🌐 Precise web control.** In Chrome, Edge or Brave, pages are read through the DevTools protocol (`b12 button "Create app"`), including off-screen elements. Clicks and typing still use the real mouse and keyboard.
- **🔁 Routines.** Record a task once, add `{{placeholders}}`, and replay it with one call.
- **🔐 Ask-first permissions.** Deleting, paying, sending, installing, signing out, closing windows, typing into password fields and opening terminals all show an **Allow once / Allow for session / Deny** dialog. An unanswered dialog is denied after 60 s. Everything is logged to `~/.deskpilot/audit.jsonl`.
- **🙈 Privacy.**
  - API keys, tokens, card numbers and IBANs are redacted before the model sees them.
  - Password fields show as `***`.
  - Password-manager and security windows are never read, captured or clicked.

## Requirements

- Windows 11 (tested), 64-bit
- [uv](https://docs.astral.sh/uv/), which installs Python 3.11+ for you
- An MCP-capable AI client (see [Connect your AI tool](#connect-your-ai-tool))

## Install

### Easiest: let Claude install it for you

Open **Claude Code** (the terminal app, the **Code** tab in the Claude desktop app, or the VS Code extension; it works with a Pro plan). Paste this prompt:

```text
Install the DeskPilot MCP server on this Windows PC from https://github.com/iamtahiralvi/DeskPilot

1. If `uv` is not installed, install it:
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
2. Install DeskPilot:
   uv tool install --python 3.13 git+https://github.com/iamtahiralvi/DeskPilot
3. Register it with Claude Code for all projects:
   claude mcp add deskpilot --scope user -- "%USERPROFILE%\.local\bin\deskpilot.exe" serve
   (expand %USERPROFILE% to the real path)
4. Install the skill: download
   https://raw.githubusercontent.com/iamtahiralvi/DeskPilot/main/skills/deskpilot/SKILL.md
   to %USERPROFILE%\.claude\skills\deskpilot\SKILL.md
5. If the Claude desktop app is installed (%APPDATA%\Claude\claude_desktop_config.json exists), also add
   "deskpilot": { "command": "<full path to deskpilot.exe>", "args": ["serve"] } under "mcpServers",
   keeping every existing entry.
6. Run `deskpilot doctor` and show me the output.
Then tell me to restart Claude (or run /mcp) so the new tools load.
```

Claude runs the commands and asks your permission for each one. When it's done, restart Claude. Then try something like *"Open Notepad and type hello"*. The screen picker appears the first time.

> Claude Desktop's chat on its own can't run install commands, so do the install from Claude Code, as above. After that, the DeskPilot tools also work in Claude Desktop chats (step 5).

### Option A: installer script

```powershell
git clone https://github.com/iamtahiralvi/DeskPilot.git
cd DeskPilot
powershell -ExecutionPolicy Bypass -File install.ps1
```

The script:
- installs `uv` if it's missing
- installs the `deskpilot` command
- creates the default permission rules
- registers DeskPilot with **Claude Code**
- installs the Claude Code skill
- prints config snippets for other tools

### Option B: install with uv yourself

```powershell
uv tool install --python 3.13 git+https://github.com/iamtahiralvi/DeskPilot
```

This puts `deskpilot.exe` in `%USERPROFILE%\.local\bin`. Run `uv tool update-shell` once if that folder isn't on your `PATH`.

Then check that it works:

```powershell
deskpilot doctor     # lists your screens and checks Windows OCR
```

### Option C: run without installing (uvx)

You can use `uvx --from git+https://github.com/iamtahiralvi/DeskPilot deskpilot serve` as the command in any client below. The first start is slower because uv builds it.

---

## Connect your AI tool

DeskPilot is a standard **stdio** MCP server. In every client, the command is `deskpilot` with the argument `serve`.

> **Tip:** Desktop apps such as Claude Desktop and Cursor sometimes don't see your `PATH`. If the server doesn't start, use the full path instead: `C:\Users\<you>\.local\bin\deskpilot.exe` (in JSON: `C:\\Users\\<you>\\.local\\bin\\deskpilot.exe`).

### Claude Code

```powershell
claude mcp add deskpilot --scope user -- deskpilot serve
```

Optional: install the bundled skill, which teaches Claude the cheapest way to use the tools.

```powershell
mkdir "$env:USERPROFILE\.claude\skills\deskpilot" -Force
copy skills\deskpilot\SKILL.md "$env:USERPROFILE\.claude\skills\deskpilot\"
```

Restart Claude Code, or run `/mcp` to reconnect.

### Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "deskpilot": { "command": "deskpilot", "args": ["serve"] }
  }
}
```

Restart Claude Desktop.

### OpenAI Codex CLI

```powershell
codex mcp add deskpilot -- deskpilot serve
```

Or add it to `~/.codex/config.toml`:

```toml
[mcp_servers.deskpilot]
command = "deskpilot"
args = ["serve"]
startup_timeout_sec = 30
tool_timeout_sec = 300
```

### Cursor

Add it to `%USERPROFILE%\.cursor\mcp.json` (global) or `.cursor/mcp.json` (one project):

```json
{
  "mcpServers": {
    "deskpilot": { "command": "deskpilot", "args": ["serve"] }
  }
}
```

### VS Code (GitHub Copilot agent mode)

```powershell
code --add-mcp '{\"name\":\"deskpilot\",\"command\":\"deskpilot\",\"args\":[\"serve\"]}'
```

Or create `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "deskpilot": { "type": "stdio", "command": "deskpilot", "args": ["serve"] }
  }
}
```

### Windsurf

Edit `%USERPROFILE%\.codeium\windsurf\mcp_config.json`:

```json
{
  "mcpServers": {
    "deskpilot": { "command": "deskpilot", "args": ["serve"] }
  }
}
```

### Gemini CLI

```powershell
gemini mcp add deskpilot deskpilot serve
```

Or add the same `mcpServers` block to `%USERPROFILE%\.gemini\settings.json`.

### Any other MCP client

Use a stdio server with command `deskpilot` and args `["serve"]`. Allow a generous tool timeout (about 300 s), because permission dialogs wait for you.

---

## First run: choose your display

The first time the agent uses DeskPilot, a **screen picker** opens:

1. Pick the monitor the agent may control: **Screen 1**, **Screen 2**, … or **All screens**.
2. Pick a permission mode:
   - **Normal** asks only before risky actions.
   - **Strict** asks before every batch of actions.
3. Tick **Remember my choice** to skip the picker next time.

With two or more displays, a good setup is to give the agent one screen and keep working on the other. The control panel then appears on your screen, not the agent's.

To change the screen later:
- run `deskpilot pick`, or
- ask the agent to call `screen_info(op="pick")`.

## Using it

Just ask your agent, for example:

> Open the Microsoft Store and show me the About page of WhatsApp.

> In File Explorer, create a folder called "Invoices 2026" on my Desktop.

> Go to my dashboard in Chrome and download last month's report.

While it works:

| You want to… | Do this |
|---|---|
| Pause / resume | **Ctrl+Alt+Esc**, or **Pause** on the panel |
| Do something yourself | **Take over**, then **Give back** |
| End the task | **Stop** (click **Unlock** later to allow it again) |
| Approve a risky step | **Allow once** / **Allow for session** in the dialog |

## Browser setup (optional, for precise web control)

Use either or both:

1. **Your everyday Chrome or Edge.**
   1. Open `chrome://inspect/#remote-debugging` and turn remote debugging on.
   2. The first time the agent reads a page in each session, the browser asks **"Allow remote debugging?"**. You click **Allow**; the agent is never allowed to click it.

   To turn this off, set `use_my_browser: false` in the policy file.
2. **The DeskPilot browser.** Ask the agent to open `agent browser https://example.com`. This uses a separate Chrome profile. Sign in once and it stays signed in.

Without either, pages are still readable through Windows UI Automation, but less precisely.

## Tools

10 tools, about 1.6k tokens of definitions:

| Tool | What it does |
|---|---|
| `screen_info` | Screens, the controlled screen, mode. `pick` reopens the picker; `switch` changes the screen (asks you) |
| `snapshot` | Text list of elements with refs. Scopes: `window`, `page`, `screen`, `taskbar`, `ocr` |
| `find` | Find elements by text or role, across the web page, the accessibility tree and OCR |
| `act` | A batch of steps (click, dblclick, rclick, hover, type, key, scroll, drag, wait, sleep, focus, launch). Returns a diff |
| `launch` | Open an app, URL, URI, file or folder, or `agent browser <url>` |
| `window` | list, focus, minimize, maximize, restore, close, move_here |
| `clipboard` | Get or set clipboard text |
| `routine` | list, run, start, save, show or delete saved routines |
| `screenshot` | Last-resort image: crop to a ref or a region, or draw ref marks |
| `done` | Task finished: hides the border, tag and panel |

## Configuration

Rules live in `~/.deskpilot/policy.yaml`. `deskpilot policy` prints its path. Edit the file, then restart your agent.

| Key | Meaning |
|---|---|
| `ask_click_words`, `ask_click_words_extra` | Click targets that need your OK (delete, pay, send, install…) |
| `deny_click_words` | Click targets that are always refused |
| `ask_keys` | Key combos that need your OK (`alt+f4`, `ctrl+w`…) |
| `ask_launch` | Programs that need your OK (terminals, regedit…) |
| `ask_close_window`, `ask_password_typing`, `ask_switch_screen` | Ask before these actions |
| `confirm_timeout_seconds` | Auto-deny an unanswered dialog after this long (default 60) |
| `private_windows` | Windows that are never read, captured or clicked |
| `redact_secrets` | Redact keys, tokens and card numbers from what the model sees |
| `use_my_browser` | Also read your everyday Chrome or Edge (needs remote debugging) |
| `hide_overlay_after_idle_seconds` | Hide the overlay when the agent goes quiet (default 30, `0` = never) |

Other files in `~/.deskpilot/`:
- `session.json`: the chosen screen and mode
- `routines/*.json`: saved routines
- `audit.jsonl`: the action log

## CLI

```
deskpilot serve    run the MCP server over stdio (your AI client runs this)
deskpilot pick     choose the screen and permission mode
deskpilot policy   create or print the path of the rules file
deskpilot doctor   list screens and check that OCR works
```

## Safety

DeskPilot controls your **real** mouse and keyboard. Before you let an agent run unattended:

- Give it a screen with nothing sensitive open, or use **Strict** mode.
- Keep the ask-first rules on, and add your own words to `ask_click_words_extra`.
- Remember that **Ctrl+Alt+Esc** always pauses it.

AI agents make mistakes. You are responsible for what you let them do on your computer.

## Development

```powershell
git clone https://github.com/iamtahiralvi/DeskPilot.git
cd DeskPilot
uv sync
uv run --group dev pytest
uv run deskpilot doctor
```

After you change the code, reinstall the global command (`--reinstall` makes uv rebuild instead of reusing a cached build):

```powershell
uv tool install --force --reinstall --python 3.13 .
```

Built with:
- the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- the raw UI Automation COM API, with cache requests so a whole window is read in one call
- the Chrome DevTools Protocol
- `mss` and `Pillow`
- `pywin32`
- Windows OCR through `winrt`
- `customtkinter` for the picker, dialogs and control panel

Issues and pull requests are welcome.

## License

[MIT](LICENSE). Free to use, change and share for any purpose, including commercial use.
