@echo off
rem <sync root>/shared/tools/run_snapshot.cmd  <DEVICE>
rem Runs the Session Hub branch snapshot collector with whichever Python works on this PC.
rem v3 (2026-09-23): creates snapshots\<DEVICE>\ before writing the log (v1 failed on a fresh PC),
rem passes no nickname through cmd.exe (code page mojibake; the collector derives it from DEVICE),
rem and verifies each Python candidate actually runs (a bare "py -3" launcher may have no runtime).
setlocal
set "ROOT=%~dp0."
set "DEV=%~1"
if "%DEV%"=="" (echo usage: run_snapshot.cmd A & exit /b 2)
set "OUT=%ROOT%\..\..\snapshots\%DEV%"
if not exist "%OUT%" mkdir "%OUT%"
set "PYEXE="
call :try "%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"
call :try "%LOCALAPPDATA%\hermes\.venv\Scripts\python.exe"
call :try "%LOCALAPPDATA%\hermes\venv\Scripts\python.exe"
call :try "%USERPROFILE%\.hermes\venv\Scripts\python.exe"
call :try python
call :try python3
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :try "%%~D\python.exe"
for /d %%D in ("C:\Python3*") do call :try "%%~D\python.exe"
call :try py -3
if not defined PYEXE (
  echo {"status":"ERROR","reason":"python_not_found","device":"%DEV%"} >> "%OUT%\run.log"
  echo {"status":"ERROR","reason":"python_not_found","device":"%DEV%"}
  exit /b 9009
)
rem run.log is written by the collector itself; Python tracebacks (if any) go to run.err
%PYEXE% "%ROOT%\session_snapshot.py" --device %DEV% --sync-root "%ROOT%\..\.." 2>> "%OUT%\run.err"
exit /b %ERRORLEVEL%

:try
if defined PYEXE goto :eof
%* -c "import sqlite3, sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if not errorlevel 1 set "PYEXE=%*"
goto :eof
