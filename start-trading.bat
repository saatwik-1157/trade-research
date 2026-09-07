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

echo Starting overnight harvest session - demo account, stops at 06:00
echo Everything still open at 06:00 is closed at what it is worth.
echo.

python tools\run_overnight.py %*

echo.
echo Session ended. For the read that matters, run:
echo     python tools\track_record.py --merge
echo.
pause
