@echo off
rem <sync root>/shared/tools/register_snapshot_task.cmd  <DEVICE> [MINUTES]
rem Registers a Windows scheduled task that runs run_snapshot.cmd every N minutes (default 15).
rem v2 (2026-09-23): nickname argument dropped (collector derives it from DEVICE).
setlocal
set "ROOT=%~dp0."
set "DEV=%~1"
set "MIN=%~2"
if "%MIN%"=="" set "MIN=15"
if "%DEV%"=="" (echo usage: register_snapshot_task.cmd A 15 & exit /b 2)
if not exist "%ROOT%\..\..\snapshots\%DEV%" mkdir "%ROOT%\..\..\snapshots\%DEV%"
schtasks /Create /F /TN "HermesSessionHub_Snapshot_%DEV%" /SC MINUTE /MO %MIN% /TR "cmd /c \"\"%ROOT%\run_snapshot.cmd\" %DEV%\""
schtasks /Run /TN "HermesSessionHub_Snapshot_%DEV%"
echo registered HermesSessionHub_Snapshot_%DEV% every %MIN% min; first run started
