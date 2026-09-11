@echo off
REM Starts the trade-research overnight harvest session on the MT5 DEMO account.
REM
REM Portable: it runs from wherever this file sits, so a copy of the repository
REM on another machine needs no edit. The Desktop shortcut on the original
REM machine hard-codes C:\Users\Asus\trade-research and does not.
REM
REM Before the first run on a new machine, check three things:
REM   1. MetaTrader 5 is installed, running and LOGGED IN to the demo account.
REM   2. Its Algo Trading toggle is ON. Off means every order is refused
REM      immediately and the session exits with code 1 for no visible reason.
REM   3. pip install -r requirements.txt, then MetaTrader5 and psutil.
REM
REM NEVER run this on two machines against the same MT5 account. The
REM single-session guard scans local processes and cannot see another laptop.
REM
REM Close this window or press Ctrl+C to stop the session early.

title trade-research overnight session
cd /d "%~dp0"

REM -------------------------------------------------------------- interpreter
REM `python` is whatever PATH says today, and that is not a stable answer. This
REM machine lists Python312 ahead of Python314 in the user PATH while the
REM toolkit's dependencies are installed only on 3.14, so a window opened after
REM that PATH entry appeared picked 3.12 and the session died on `import numpy`
REM three imports in -- after printing "Starting overnight harvest session", so
REM it read like a session that had begun.
REM
REM Choose by asking each interpreter whether it can import what the session
REM needs, rather than by trusting a name. Requirement 3 above is the thing
REM being checked, so a machine that never met it gets told which package is
REM missing instead of a traceback from inside a module it has never heard of.
setlocal
set "TR_PYTHON="
for %%C in (
  "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
) do if not defined TR_PYTHON if exist %%C (
  %%C -c "import numpy, pandas, requests, MetaTrader5, psutil" >nul 2>&1
  if not errorlevel 1 set "TR_PYTHON=%%~C"
)
if not defined TR_PYTHON (
  python -c "import numpy, pandas, requests, MetaTrader5, psutil" >nul 2>&1
  if not errorlevel 1 set "TR_PYTHON=python"
)
if not defined TR_PYTHON (
  echo.
  echo   NO USABLE PYTHON. Nothing was started and no order was sent.
  echo.
  echo   Every interpreter found is missing at least one of numpy, pandas,
  echo   requests, MetaTrader5 or psutil. Install them into ONE of them and
  echo   run this again -- the first that can import all five is the one used:
  echo.
  echo       "%%LOCALAPPDATA%%\Programs\Python\Python314\python.exe" -m pip install -r requirements.txt
  echo       "%%LOCALAPPDATA%%\Programs\Python\Python314\python.exe" -m pip install MetaTrader5 psutil
  echo.
  echo   psutil is the one worth checking twice: absent, run_overnight.py
  echo   cannot see a session already running on this machine and proceeds
  echo   UNGUARDED, and two harvest loops on one account double the risk.
  echo.
  pause
  exit /b 1
)

echo Starting overnight harvest session - demo account, stops at 06:00
echo Everything still open at 06:00 is closed at what it is worth.
echo Python: %TR_PYTHON%
echo.

REM --detach: run with no console at all.
REM
REM Eight sessions in a row ended between one 20-second tick and the next,
REM with no traceback and no KeyboardInterrupt, and the finally that
REM releases the keep-awake hold never ran. That is an externally
REM terminated process, and the most likely outside is this window closing.
REM
REM run_overnight.py now installs a console handler, which turns a close
REM into a wind-down -- but Windows allows only a few seconds for that and
REM closing seven positions may not fit. Detaching is the stronger fix:
REM with pythonw there is no console to close, so the event never arrives.
REM The session log holds everything the window would have shown.
set "TR_ARGS=%*"
set "TR_DETACH="
if not "%TR_ARGS%"=="%TR_ARGS:--detach=%" set "TR_DETACH=1"

if not defined TR_DETACH goto :attached

REM ------------------------------------------------------------ detached run
REM FLAT, not inside `if defined TR_DETACH ( ... )`, and that is the fix rather
REM than a matter of style.
REM
REM cmd expands %VAR% in a parenthesised block when it PARSES the block, before
REM a single line inside it runs. So `call set "TR_PYTHONW=..."` set the
REM variable at runtime while the `start` two lines below had already been
REM expanded with the value it held at parse time: nothing. `start` was handed
REM an EMPTY program name, launched no process, and the script went on to print
REM "Session launched; it is not tied to this window" and exit 0.
REM
REM Measured 2026-09-11, the first time this path was used after it was
REM written: no pythonw process, an empty detach-launch.log, no session log and
REM no CRASH_REPORTS record. `setlocal enabledelayedexpansion` with !VAR! would
REM also fix it; a goto has no second expansion mode to forget about.
call set "TR_ARGS=%%TR_ARGS:--detach=%%"
call set "TR_PYTHONW=%%TR_PYTHON:python.exe=pythonw.exe%%"
if not exist "%TR_PYTHONW%" set "TR_PYTHONW=%TR_PYTHON%"
for %%I in ("%TR_PYTHONW%") do set "TR_IMAGE=%%~nxI"

echo Starting DETACHED - this window can be closed safely.
echo Python: %TR_PYTHONW%
echo.
REM Redirected, and NOT to NUL. pythonw.exe launched with no valid stdout
REM handle dies on its first print, and because it has no console the
REM traceback goes nowhere -- the session log gets its header line and
REM then nothing. Measured 2026-09-10: a detached launch exited inside 8
REM seconds and left a one-line log. Giving it a file fixes the handle and
REM catches any startup failure that happens before logging is up.
start "trade-research session" /B "%TR_PYTHONW%" tools\run_overnight.py %TR_ARGS% > "logs\detach-launch.log" 2>&1

REM PROVE it started before saying so. The bug above printed a success line
REM over a launch that started nothing, which is the same failure as a session
REM log that gets its header and then stops: a report of success is not
REM evidence of one. Six seconds is past run_overnight's own preflight, so a
REM process still alive here has got as far as its first pass.
timeout /t 6 /nobreak >nul
tasklist /FI "IMAGENAME eq %TR_IMAGE%" 2>nul | find /I "%TR_IMAGE%" >nul
if errorlevel 1 goto :detach_failed

echo Session launched; it is not tied to this window.
echo     log:    logs\overnight-*.log
echo     record: CRASH_REPORTS\session-*.json
echo     stop:   taskkill /IM %TR_IMAGE%
echo.
pause
exit /b 0

:detach_failed
echo.
echo   THE SESSION DID NOT START. No %TR_IMAGE% is running, and no order was
echo   sent. Nothing is holding a position and nothing will flush at 06:00.
echo.
echo   Whatever the launch managed to print is in logs\detach-launch.log:
echo.
type "logs\detach-launch.log"
echo.
pause
exit /b 1

:attached

echo Closing this window asks the session to wind down rather than killing
echo it outright. Use --detach to run with no console at all.
echo.
"%TR_PYTHON%" tools\run_overnight.py %*

:done

echo.
echo Session ended. For the read that matters, run:
echo     "%TR_PYTHON%" tools\track_record.py --merge
echo.
pause
