# centen installer for Windows.
# - creates a Python virtual environment in .venv next to this script and installs centen into it
# - keeps config, key and database in the "data" folder next to this script (CENTEN_HOME)
# - moves the Enable Banking private key from Downloads into data\ if it is still there
# - tests the Enable Banking connection
# - registers centen as an MCP server in Claude Desktop (after backing up the config)
# Run:  powershell -ExecutionPolicy Bypass -File C:\centen\install.ps1      (add -NoLink to skip bank linking)
param([switch]$NoLink, [switch]$NoClaudeConfig)

$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
$Data = Join-Path $Root "data"
$Venv = Join-Path $Root ".venv"
$Exe = Join-Path $Venv "Scripts\centen.exe"
New-Item -ItemType Directory -Force -Path $Data | Out-Null
$env:CENTEN_HOME = $Data

function Step($msg) { Write-Host ""; Write-Host "== $msg ==" -ForegroundColor Cyan }
function Fail($msg) { Write-Host $msg -ForegroundColor Red; Read-Host "Press Enter to close"; exit 1 }

# 1. Python 3.10+
Step "Looking for Python 3.10 or newer"
$Python = $null
$candidates = @(@{Exe = "py"; Args = @("-3")}, @{Exe = "python"; Args = @()}, @{Exe = "python3"; Args = @()})
foreach ($c in $candidates) {
    if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
    $out = & $c.Exe @($c.Args + @("-c", "import sys; print(sys.version_info[0]*100+sys.version_info[1])")) 2>$null
    if ($LASTEXITCODE -eq 0) {
        $n = 0
        if ([int]::TryParse(("$out").Trim(), [ref]$n) -and $n -ge 310) { $Python = $c; break }
    }
}
if (-not $Python) { Fail "No Python 3.10+ found. Install it from https://www.python.org/downloads/ (tick 'Add to PATH') and run this again." }
$pyPath = & $Python.Exe @($Python.Args + @("-c", "import sys; print(sys.executable)"))
Write-Host "Using $pyPath"

# 2. Virtual environment + install
Step "Installing centen into $Venv"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    & $Python.Exe @($Python.Args + @("-m", "venv", $Venv))
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the virtual environment." }
}
$VPy = Join-Path $Venv "Scripts\python.exe"
& $VPy -m pip install --quiet --disable-pip-version-check -e $Root
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Exe)) { Fail "pip install failed (see the messages above)." }
& $Exe --version

# 3. Private key
Step "Checking the Enable Banking private key"
$cfgFile = Join-Path $Data "config.json"
if (-not (Test-Path $cfgFile)) { Fail "Missing $cfgFile" }
$cfg = Get-Content $cfgFile -Raw | ConvertFrom-Json
$appId = $cfg.enablebanking.app_id
$key = $cfg.enablebanking.private_key_path
if (-not (Test-Path $key)) {
    $downloads = Join-Path $env:USERPROFILE "Downloads"
    $found = Get-ChildItem $downloads -Filter "$appId*.pem" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($found) {
        Move-Item $found.FullName $key
        Write-Host "Moved $($found.Name) from Downloads to $key"
    } else {
        Fail "Key not found. Put $appId.pem in $Data and run this again."
    }
} else { Write-Host "Found $key" }

# 4. Test the connection
Step "Testing the Enable Banking connection"
& $Exe init

# 5. Claude Desktop
if (-not $NoClaudeConfig) {
    Step "Registering centen in Claude Desktop"
    $paths = @(Join-Path $env:APPDATA "Claude\claude_desktop_config.json")
    $pkgRoot = Join-Path $env:LOCALAPPDATA "Packages"
    Get-ChildItem $pkgRoot -Directory -Filter "*Claude*" -ErrorAction SilentlyContinue | ForEach-Object {
        $paths += (Join-Path $_.FullName "LocalCache\Roaming\Claude\claude_desktop_config.json")
    }
    $targets = @($paths | Where-Object { Test-Path $_ })
    if ($targets.Count -eq 0 -and (Test-Path (Split-Path $paths[0]))) { $targets = @($paths[0]) }
    if ($targets.Count -eq 0) {
        Write-Host "Claude Desktop config folder not found. Add this under mcpServers via Settings > Developer > Edit Config:"
    }
    $entry = [pscustomobject]@{ command = $Exe; args = @("serve"); env = [pscustomobject]@{ CENTEN_HOME = $Data } }
    foreach ($p in $targets) {
        $json = $null
        if (Test-Path $p) {
            Copy-Item $p "$p.bak-centen" -Force
            $raw = Get-Content $p -Raw
            if ($raw -and $raw.Trim()) { $json = $raw | ConvertFrom-Json }
        }
        if (-not $json) { $json = [pscustomobject]@{} }
        if (-not $json.PSObject.Properties["mcpServers"]) { $json | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) }
        if ($json.mcpServers.PSObject.Properties["centen"]) { $json.mcpServers.centen = $entry }
        else { $json.mcpServers | Add-Member -NotePropertyName centen -NotePropertyValue $entry }
        $text = $json | ConvertTo-Json -Depth 32
        [System.IO.File]::WriteAllText($p, $text, (New-Object System.Text.UTF8Encoding $false))
        Write-Host "Updated $p (backup: $p.bak-centen)"
    }
    Write-Host ([pscustomobject]@{ mcpServers = [pscustomobject]@{ centen = $entry } } | ConvertTo-Json -Depth 5)
    Write-Host "Fully quit Claude Desktop (system tray > Quit) and reopen it to load centen." -ForegroundColor Yellow
}

# 6. Link banks
if (-not $NoLink) {
    Step "Linking your banks"
    Write-Host "For each bank: approve in your bank app, then copy the address of the 'can't connect' page"
    Write-Host "(https://127.0.0.1:8765/callback?...code=...) and paste it here."
    foreach ($bank in @("KBC", "Belfius", "Crelan", "Bank Van Breda")) {
        $ans = Read-Host "Link $bank now? [Y/n]"
        if ($ans -and $ans.Trim().ToLower().StartsWith("n")) { continue }
        & $Exe link --bank $bank
    }
}

Step "Done"
Write-Host "Use $Root\centen.cmd for commands, e.g.:  $Root\centen.cmd sync   or   $Root\centen.cmd accounts"
Read-Host "Press Enter to close"
