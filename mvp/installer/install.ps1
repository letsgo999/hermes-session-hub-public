<#
.SYNOPSIS
  Installs (or updates) the Hermes Session Hub plugin for the current Windows user.

.DESCRIPTION
  1. Finds the Hermes home folder (-HermesHome, then HERMES_HOME, then %LOCALAPPDATA%\hermes).
  2. Backs up an existing plugins\session-hub folder to <HermesHome>\session-hub\backups\.
  3. Copies the plugin package into <HermesHome>\plugins\session-hub\.
  4. Enables the Python half with "hermes plugins enable session-hub" (skip with -NoEnable).
  5. Optional: registers the Codex/Claude collector as a scheduled task (-EnableCollector).
  6. Optional: writes a starter session-hub.config.json (-WriteConfig).

  Nothing in Hermes' state.db or Kanban databases is modified. Run uninstall.ps1 to undo.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -EnableCollector
#>
[CmdletBinding()]
param(
    [string]$HermesHome,
    [switch]$EnableCollector,
    [ValidateRange(5, 1440)][int]$CollectorMinutes = 30,
    [switch]$WriteConfig,
    [switch]$NoEnable,
    [switch]$UpdateProfileCopies,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$PluginName = 'session-hub'
$TaskName = 'Hermes_SessionHub_AI_Collector'

function Write-Step([string]$Message) { Write-Host "[session-hub] $Message" }

function Resolve-HermesHome([string]$Explicit) {
    $value = $Explicit
    if (-not $value) { $value = $env:HERMES_HOME }
    if ($value) {
        $item = [System.IO.DirectoryInfo]::new([Environment]::ExpandEnvironmentVariables($value))
        if ($item.Parent -and $item.Parent.Name -ieq 'profiles' -and $item.Parent.Parent) {
            $base = $item.Parent.Parent.FullName
            if ((Test-Path -LiteralPath (Join-Path $base 'config.yaml')) -or (Test-Path -LiteralPath (Join-Path $base 'state.db'))) { return $base }
        }
        return $item.FullName
    }
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is not set; pass -HermesHome.' }
    return (Join-Path $env:LOCALAPPDATA 'hermes')
}

function Find-PluginSource {
    $candidates = @(
        (Join-Path $PSScriptRoot 'plugins\session-hub'),            # release zip / dist folder
        (Join-Path $PSScriptRoot '..\plugins\session-hub'),         # repository checkout: source package
        (Join-Path $PSScriptRoot '..\dist\plugins\session-hub')     # repository: built package (fallback)
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $candidate 'plugin.yaml')) { return (Resolve-Path -LiteralPath $candidate).ProviderPath }
    }
    throw 'Plugin package not found next to install.ps1 (expected plugins\session-hub\plugin.yaml).'
}

function Find-Python([string]$Root, [switch]$Windowless) {
    $exe = if ($Windowless) { 'pythonw.exe' } else { 'python.exe' }
    $candidates = @(
        (Join-Path $Root "hermes-agent\venv\Scripts\$exe"),
        (Join-Path $Root ".venv\Scripts\$exe"),
        (Join-Path $Root "venv\Scripts\$exe")
    )
    foreach ($candidate in $candidates) { if (Test-Path -LiteralPath $candidate) { return $candidate } }
    $command = Get-Command ($exe -replace '\.exe$', '') -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Copy-Package([string]$Source, [string]$Target) {
    if ($DryRun) { Write-Step "DRY RUN: would copy $Source -> $Target"; return }
    # Copy into a staging folder first, then swap, so a failed copy never leaves a half-written plugin.
    $staging = "$Target.staging"
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    Get-ChildItem -LiteralPath $Source -Recurse -File |
        Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -ne '.pyc' } |
        ForEach-Object {
            $relative = $_.FullName.Substring($Source.Length).TrimStart('\', '/')
            $destination = Join-Path $staging $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
        }
    if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
    Move-Item -LiteralPath $staging -Destination $Target
}

$hermesRoot = Resolve-HermesHome $HermesHome
$source = Find-PluginSource
$version = (Select-String -LiteralPath (Join-Path $source 'plugin.yaml') -Pattern '^version:\s*(.+)$').Matches[0].Groups[1].Value.Trim()
Write-Step "Hermes home : $hermesRoot"
Write-Step "Package     : $source (version $version)"
if (-not (Test-Path -LiteralPath $hermesRoot)) { throw "Hermes home not found: $hermesRoot (install Hermes first or pass -HermesHome)." }

$pluginsRoot = Join-Path $hermesRoot 'plugins'
$target = Join-Path $pluginsRoot $PluginName
$dataRoot = Join-Path $hermesRoot 'session-hub'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

if (-not $DryRun) { New-Item -ItemType Directory -Path $pluginsRoot, $dataRoot -Force | Out-Null }

# 1) Backup + copy
if (Test-Path -LiteralPath $target) {
    $backup = Join-Path $dataRoot "backups\$PluginName-$stamp"
    Write-Step "Backing up existing plugin -> $backup"
    if (-not $DryRun) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
        Copy-Item -LiteralPath $target -Destination $backup -Recurse -Force
    }
}
Copy-Package $source $target
Write-Step "Installed plugin -> $target"

# 2) Per-profile plugin copies (each Hermes profile can carry its own plugins folder)
$profileCopies = @(Get-ChildItem -LiteralPath (Join-Path $hermesRoot 'profiles') -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { Join-Path $_.FullName "plugins\$PluginName" } |
    Where-Object { Test-Path -LiteralPath (Join-Path $_ 'plugin.yaml') })
foreach ($copy in $profileCopies) {
    if ($UpdateProfileCopies) {
        Copy-Package $source $copy
        Write-Step "Updated profile copy -> $copy"
    } else {
        Write-Step "Note: older profile copy found at $copy (re-run with -UpdateProfileCopies to update it)."
    }
}

# 3) Optional starter settings
$configPath = Join-Path $dataRoot 'session-hub.config.json'
if ($WriteConfig) {
    $example = Join-Path $PSScriptRoot 'session-hub.config.example.json'
    if (Test-Path -LiteralPath $configPath) {
        Write-Step "Settings file already exists, left unchanged: $configPath"
    } elseif (Test-Path -LiteralPath $example) {
        if (-not $DryRun) { Copy-Item -LiteralPath $example -Destination $configPath }
        Write-Step "Wrote starter settings -> $configPath (edit labels/branches, then restart Hermes)"
    }
}

# 4) Enable the Python half
if (-not $NoEnable) {
    $hermes = Get-Command hermes -ErrorAction SilentlyContinue
    if ($hermes -and -not $DryRun) {
        Write-Step 'Running: hermes plugins enable session-hub'
        try {
            & $hermes.Source plugins enable $PluginName
            if ($LASTEXITCODE -ne 0) { Write-Warning 'hermes plugins enable failed; enable it manually (hermes plugins).' }
        } catch {
            Write-Warning "hermes plugins enable failed ($_); enable it manually (hermes plugins)."
        }
    } elseif (-not $hermes) {
        Write-Warning "'hermes' CLI not on PATH. Enable the plugin manually: hermes plugins enable session-hub"
    }
}

# 5) Optional Codex/Claude collector schedule (per-user task, no admin rights needed)
if ($EnableCollector) {
    $python = Find-Python $hermesRoot -Windowless
    if (-not $python) { $python = Find-Python $hermesRoot }
    if (-not $python) {
        Write-Warning 'Python not found; collector task not registered. The tab Refresh button still collects on demand.'
    } else {
        $script = Join-Path $target 'dashboard\ai_sessions.py'
        $arguments = '"{0}" --collect --hermes-home "{1}"' -f $script, $hermesRoot
        Write-Step "Registering scheduled task $TaskName every $CollectorMinutes min ($python)"
        if (-not $DryRun) {
            try {
                $action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory (Split-Path -Parent $script)
                $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
                    -RepetitionInterval (New-TimeSpan -Minutes $CollectorMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
                $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
                    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
                Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
                    -Description 'Hermes Session Hub: collect Codex/Claude session summaries' -Force | Out-Null
            } catch {
                Write-Warning "Could not register ${TaskName}: $_"
            }
            try { Start-ScheduledTask -TaskName $TaskName } catch { Write-Warning "Registered ${TaskName}, but the first run did not start: $_" }
        }
    }
}

Write-Step 'Done. Next:'
Write-Host '  1) Restart Hermes (the backend loads plugin_api.py at start).'
Write-Host '  2) Hermes Desktop -> Capabilities -> Plugins -> enable "Session Hub", then Rescan/hot reload.'
Write-Host "  3) Optional settings: $configPath (see session-hub.config.example.json)."
