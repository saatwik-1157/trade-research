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

if defined TR_DETACH (
  call set "TR_ARGS=%%TR_ARGS:--detach=%%"
  call set "TR_PYTHONW=%%TR_PYTHON:python.exe=pythonw.exe%%"
  if not exist "%TR_PYTHONW%" set "TR_PYTHONW=%TR_PYTHON%"
  echo Starting DETACHED - this window can be closed safely.
  echo Python: %TR_PYTHONW%
  echo.
  start "trade-research session" /B "%TR_PYTHONW%" tools\run_overnight.py %TR_ARGS%
  echo Session launched; it is not tied to this window.
  echo     log:    logs\overnight-*.log
  echo     record: CRASH_REPORTS\session-*.json
  echo     stop:   taskkill /IM pythonw.exe
  echo.
  pause
  exit /b 0
)

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
