<#
.SYNOPSIS
  Removes the Hermes Session Hub plugin for the current Windows user.

.DESCRIPTION
  Disables the plugin ("hermes plugins disable session-hub"), removes the collector scheduled task,
  and moves <HermesHome>\plugins\session-hub into <HermesHome>\session-hub\backups\ (nothing is
  deleted outright). Settings, the project registry and caches under <HermesHome>\session-hub\ are
  kept unless -RemoveData is given, in which case that folder is moved aside as well.
#>
[CmdletBinding()]
param(
    [string]$HermesHome,
    [switch]$RemoveData,
    [switch]$IncludeProfileCopies,
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

$hermesRoot = Resolve-HermesHome $HermesHome
$target = Join-Path $hermesRoot "plugins\$PluginName"
$dataRoot = Join-Path $hermesRoot 'session-hub'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Write-Step "Hermes home : $hermesRoot"

$hermes = Get-Command hermes -ErrorAction SilentlyContinue
if ($hermes -and -not $DryRun) {
    try {
        & $hermes.Source plugins disable $PluginName
        if ($LASTEXITCODE -ne 0) { Write-Warning 'hermes plugins disable failed; disable it manually (hermes plugins).' }
    } catch {
        Write-Warning "hermes plugins disable failed: $_"
    }
} elseif (-not $hermes) {
    Write-Warning "'hermes' CLI not on PATH; disable the plugin manually (hermes plugins disable session-hub)."
}

$task = $null
try { $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { $task = $null }
if ($task) {
    Write-Step "Removing scheduled task $TaskName"
    if (-not $DryRun) {
        try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false } catch { Write-Warning "Could not remove ${TaskName}: $_" }
    }
}

if (Test-Path -LiteralPath $target) {
    $backup = Join-Path $dataRoot "backups\$PluginName-removed-$stamp"
    Write-Step "Moving plugin folder -> $backup"
    if (-not $DryRun) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
        Move-Item -LiteralPath $target -Destination $backup
    }
} else {
    Write-Step "Plugin folder not found: $target"
}

$profileCopies = @(Get-ChildItem -LiteralPath (Join-Path $hermesRoot 'profiles') -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { Join-Path $_.FullName "plugins\$PluginName" } |
    Where-Object { Test-Path -LiteralPath (Join-Path $_ 'plugin.yaml') })
foreach ($copy in $profileCopies) {
    if ($IncludeProfileCopies) {
        $profileName = Split-Path -Leaf (Split-Path -Parent (Split-Path -Parent $copy))
        $aside = Join-Path $dataRoot "backups\$PluginName-removed-$stamp-$profileName"
        Write-Step "Moving profile copy -> $aside"
        if (-not $DryRun) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $aside) -Force | Out-Null
            Move-Item -LiteralPath $copy -Destination $aside
        }
    } else {
        Write-Step "Note: profile copy left in place: $copy (re-run with -IncludeProfileCopies to move it too)."
    }
}

if ($RemoveData -and (Test-Path -LiteralPath $dataRoot)) {
    $aside = Join-Path $hermesRoot "session-hub-removed-$stamp"
    Write-Step "Moving Session Hub data folder -> $aside (delete it yourself when sure)"
    if (-not $DryRun) { Move-Item -LiteralPath $dataRoot -Destination $aside }
}

Write-Step 'Done. Restart Hermes and turn the Desktop half off in Capabilities -> Plugins if it is still listed.'
