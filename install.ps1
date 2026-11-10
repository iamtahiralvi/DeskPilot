# DeskPilot installer: installs the `deskpilot` command, registers it with Claude Code,
# installs the agent skill, and prints config for other MCP clients.
# Usage:  powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Write-Host "== DeskPilot installer ==" -ForegroundColor DarkYellow

# 1. uv (Python package manager; it also fetches Python if needed)
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv..."
    powershell -ExecutionPolicy Bypass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

# 2. Install the deskpilot command globally
Write-Host "Installing deskpilot..."
uv tool install --force --python 3.13 "$root"
$exe = Join-Path $env:USERPROFILE ".local\bin\deskpilot.exe"
if (-not (Test-Path $exe)) { $exe = (Get-Command deskpilot).Source }
Write-Host "  -> $exe"

# 3. Default permission rules (~/.deskpilot/policy.yaml)
& $exe policy | Out-Null

# 4. Claude Code: register MCP server (user scope = available in every project) + skill
if (Get-Command claude -ErrorAction SilentlyContinue) {
    try { claude mcp remove deskpilot --scope user 2>$null | Out-Null } catch {}
    claude mcp add deskpilot --scope user -- "$exe" serve
    Write-Host "Registered with Claude Code." -ForegroundColor Green
} else {
    Write-Host "Claude Code CLI not found; skipped registration." -ForegroundColor Yellow
}
$skillDst = Join-Path $env:USERPROFILE ".claude\skills\deskpilot"
New-Item -ItemType Directory -Force $skillDst | Out-Null
Copy-Item (Join-Path $root "skills\deskpilot\SKILL.md") $skillDst -Force
Write-Host "Installed skill -> $skillDst" -ForegroundColor Green

# 5. Snippets for other agents
$exeJson = $exe -replace '\\', '\\'
Write-Host ""
Write-Host "Other MCP clients:" -ForegroundColor DarkYellow
Write-Host "  Codex (~/.codex/config.toml):"
Write-Host "    [mcp_servers.deskpilot]"
Write-Host "    command = '$exe'"
Write-Host "    args = ['serve']"
Write-Host "    startup_timeout_sec = 30"
Write-Host "    tool_timeout_sec = 300"
Write-Host ""
Write-Host "  Claude Desktop / Cursor / Windsurf / VS Code (mcp JSON):"
Write-Host "    { `"mcpServers`": { `"deskpilot`": { `"command`": `"$exeJson`", `"args`": [`"serve`"] } } }"
Write-Host ""
Write-Host "Done. Restart your agent, then ask it e.g. 'open Microsoft Store and show me the About page of WhatsApp'." -ForegroundColor Green
Write-Host "Choose the screen when the picker appears. Re-pick any time: deskpilot pick. Rules: ~/.deskpilot/policy.yaml"
