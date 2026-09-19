@echo off
REM trade-research - double-click menu for everything this toolkit does.
REM
REM Portable: it runs from wherever this file sits, so a copy of the repository
REM on another machine needs no edit.
REM
REM WHY A MENU AND NOT A SCRIPT PER TASK. Double-clicking a .bat gives you one
REM shot at getting it right and no console to read the error in if it closes.
REM So every path here ends at a pause, the window never closes on its own, and
REM the options that can SEND AN ORDER are marked and ask before they run.
REM
REM Three batch traps this file avoids on purpose, all of them measured in this
REM project already:
REM   * No %VAR% read inside a parenthesised block. cmd expands those when it
REM     PARSES the block, before a single line inside it runs, which on
REM     2026-09-11 handed `start` an empty program name and reported success
REM     over a launch that started nothing. Dispatch here is goto-based, and a
REM     goto has no second expansion mode to forget about.
REM   * FULL PATHS to System32 tools. Launched from a shell whose PATH carries
REM     Git's bin, `find` and `timeout` resolve to the GNU tools and reject the
REM     Windows flags, so a check fails on its own arguments and reports a
REM     working thing as broken.
REM   * No timeout.exe. It refuses to run when stdin is not a console -- "ERROR:
REM     Input redirection is not supported" -- and returns instantly, so the
REM     wait silently does not happen. ping is used where a wait is needed.

title trade-research
cd /d "%~dp0"
setlocal

set "TR_ROOT=%~dp0"
set "SYS32=%SystemRoot%\System32"
set "TR_EMPTY=0"

REM -------------------------------------------------------------- interpreter
REM Chosen by asking each interpreter whether it can import what the toolkit
REM needs, not by trusting the name `python`. PATH order is not a stable answer:
REM this machine lists Python312 ahead of Python314 while the dependencies are
REM installed only on 3.14, and a session picked 3.12 and died on `import numpy`
REM after printing that it had started.
REM
REM The project's own .venv is asked FIRST, because it is the environment the
REM tests and every figure in the reports were produced under.
set "TR_PYTHON="
for %%C in (
  "%TR_ROOT%.venv\Scripts\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
) do if not defined TR_PYTHON if exist %%C (
  %%C -c "import numpy, pandas, requests" >nul 2>&1
  if not errorlevel 1 set "TR_PYTHON=%%~C"
)
if not defined TR_PYTHON (
  python -c "import numpy, pandas, requests" >nul 2>&1
  if not errorlevel 1 set "TR_PYTHON=python"
)
if not defined TR_PYTHON goto :no_python

REM MetaTrader5 and psutil are checked SEPARATELY and are not fatal. Half this
REM menu is research and measurement that never touches a venue, and refusing
REM to open at all because a Windows-only trading package is missing would make
REM the tool useless on the machine where you are only reading reports.
set "TR_MT5=1"
"%TR_PYTHON%" -c "import MetaTrader5, psutil" >nul 2>&1
if errorlevel 1 set "TR_MT5="

:menu
cls
echo.
echo   ================================================================
echo     trade-research
echo   ================================================================
echo.
echo     Python : %TR_PYTHON%
if defined TR_MT5 echo     MT5    : available
if not defined TR_MT5 echo     MT5    : NOT available - options 3-6 are disabled
echo.
echo   ---------------------------------------------------------------
echo     READ - none of these send an order
echo   ---------------------------------------------------------------
echo     1   Account       balance, open positions, closed-trade stats
echo     2   Track record  merge new trades and print the R-multiple
echo     3   Can I run tonight?   dry run, checks the venue's week
echo     17  PREFLIGHT     ^<-- run this before any session. Eleven checks:
echo     18  SLEEP TEST    ^<-- overnight run that sends NO ORDERS. It
echo                       proves the machine stays awake to the deadline,
echo                       which is the one thing blocking a real soak.
echo                       power, standby, algo toggle, demo fence, the
echo                       open book, the venue's week, and when it works
echo.
echo   ---------------------------------------------------------------
echo     TRADE - these send REAL orders to the DEMO account
echo   ---------------------------------------------------------------
echo     4   Overnight session          runs until 06:00, then flattens
echo     5   Overnight session DETACHED window can be closed
echo     6   Wind down NOW              close everything, open nothing
echo.
echo   ---------------------------------------------------------------
echo     MEASURE - research, no venue needed
echo   ---------------------------------------------------------------
echo     7   Analyse a stock      snapshot + verify every number
echo     8   Run the tests
echo     9   Open the logs folder
echo.
echo   ---------------------------------------------------------------
echo     TRADINGVIEW - there is no API to connect to; these are the two
echo     integrations TradingView actually supports
echo   ---------------------------------------------------------------
echo     10  Import a strategy export   measure a TV strategy against
echo                                    the live MT5 record, same stats
echo     11  Alert log                  summarise webhook alerts received
echo     12  Start the alert receiver   records alerts, NEVER trades
echo.
echo   ---------------------------------------------------------------
echo     SEARCH - does a rule work? every one so far has said no
echo   ---------------------------------------------------------------
echo     13  Volume        crypto D1, with unweighted controls  ~2 min
echo     14  Supertrend    FX H1, the one rule from outside     ~3 min
echo     15  Rule search   the 41-candidate baseline            ~5 min
echo     16  Cost hurdle   the win rate the spread demands      ~1 min
echo.
echo     0   Exit
echo.
set "CHOICE="
set /p "CHOICE=  Choose: "
REM Strip any quotes the operator typed, or the comparison below breaks on them.
if defined CHOICE set "CHOICE=%CHOICE:"=%"

REM SPIN GUARD. `set /p` at end-of-input leaves the variable untouched and
REM returns immediately, so a menu that loops on empty input loops FOREVER at
REM a rate limited only by the CPU. A human double-clicking never hits this;
REM anything piping input does, and so does a closed stdin. Found by running
REM this file with its input redirected -- it pinned a core and had to be
REM killed, which is exactly the failure a "just double-click it" tool must
REM not have hiding in it.
if defined CHOICE goto :chose
set /a TR_EMPTY += 1
if %TR_EMPTY% GEQ 3 goto :no_input
goto :menu
:chose
set "TR_EMPTY=0"

if "%CHOICE%"=="1" goto :account
if "%CHOICE%"=="2" goto :record
if "%CHOICE%"=="3" goto :dryrun
if "%CHOICE%"=="4" goto :session
if "%CHOICE%"=="5" goto :detached
if "%CHOICE%"=="6" goto :winddown
if "%CHOICE%"=="7" goto :analyse
if "%CHOICE%"=="8" goto :tests
if "%CHOICE%"=="9" goto :logs
if "%CHOICE%"=="10" goto :tvimport
if "%CHOICE%"=="11" goto :tvalerts
if "%CHOICE%"=="12" goto :tvserve
if "%CHOICE%"=="13" goto :svolume
if "%CHOICE%"=="14" goto :ssuper
if "%CHOICE%"=="15" goto :srules
if "%CHOICE%"=="16" goto :scost
if "%CHOICE%"=="17" goto :preflight
if "%CHOICE%"=="18" goto :sleeptest
if "%CHOICE%"=="0" goto :done
echo.
echo   "%CHOICE%" is not on the menu.
"%SYS32%\ping.exe" -n 2 127.0.0.1 >nul 2>&1
goto :menu

REM ------------------------------------------------------------------- read
:account
cls
echo   Reading the account. This is read-only and sends nothing.
echo.
if not defined TR_MT5 goto :need_mt5
"%TR_PYTHON%" tools\mt5_account.py
echo.
if errorlevel 1 echo   That did not succeed. If MetaTrader 5 is closed, open it and log in.
pause
goto :menu

:record
cls
echo   Merging new closed trades into the ledger, then reporting.
echo.
echo   Read the R-MULTIPLE, not the balance. The harvest loop books winners
echo   and holds losers, so a rising balance is the mechanism, not an edge.
echo.
if not defined TR_MT5 goto :need_mt5
"%TR_PYTHON%" tools\track_record.py --merge
echo.
if errorlevel 1 echo   That did not succeed. If MetaTrader 5 is closed, open it and log in.
pause
goto :menu

:dryrun
cls
echo   Dry run. Resolves the command, checks the venue's week, sends nothing.
echo.
if not defined TR_MT5 goto :need_mt5
"%TR_PYTHON%" tools\run_overnight.py --dry-run --until-hour 6
echo.
echo   A REFUSED here is the guard working. A Friday deadline lands on the
echo   venue's Saturday, where every close is rejected and the book is stuck
echo   until Monday. The refusal names the next date that works.
echo.
pause
goto :menu

REM ------------------------------------------------------------------ trade
:session
cls
echo   ================================================================
echo     THIS SENDS REAL ORDERS to the MetaTrader 5 DEMO account.
echo   ================================================================
echo.
echo     It runs until 06:00 and then CLOSES EVERYTHING still open, at
echo     whatever it is worth, losses included. That is the point of it.
echo.
echo     Closing this window asks the session to wind down rather than
echo     killing it. A watchdog is started beside it and will restart it
echo     if it dies, but never past a risk halt or the kill switch.
echo.
echo     Check first: MT5 is open, logged in, and Algo Trading is ON.
echo.
set "OK="
set /p "OK=  Type YES to start, anything else to go back: "
if defined OK set "OK=%OK:"=%"
if /i not "%OK%"=="YES" goto :menu
if not defined TR_MT5 goto :need_mt5
cls
call "%TR_ROOT%start-trading.bat"
goto :menu

:detached
cls
echo   ================================================================
echo     THIS SENDS REAL ORDERS to the MetaTrader 5 DEMO account.
echo   ================================================================
echo.
echo     Detached: the session keeps running after this window closes.
echo     It still stops and flattens at 06:00.
echo.
echo     To stop it early use the kill switch rather than Task Manager:
echo         %TR_PYTHON% tools\kill_switch.py
echo.
set "OK="
set /p "OK=  Type YES to start, anything else to go back: "
if defined OK set "OK=%OK:"=%"
if /i not "%OK%"=="YES" goto :menu
if not defined TR_MT5 goto :need_mt5
cls
call "%TR_ROOT%start-trading.bat" --detach
goto :menu

:winddown
cls
echo   ================================================================
echo     WIND DOWN - closes open positions, opens nothing new.
echo   ================================================================
echo.
echo     This is the remedy for a book left open. It is exempt from the
echo     weekend guard, because it takes no positions it cannot close.
echo.
echo     No watchdog is started for a wind-down: restarting one that died
echo     would re-enter the book it is emptying.
echo.
set "OK="
set /p "OK=  Type YES to wind down, anything else to go back: "
if defined OK set "OK=%OK:"=%"
if /i not "%OK%"=="YES" goto :menu
if not defined TR_MT5 goto :need_mt5
cls
call "%TR_ROOT%start-trading.bat" --harvest-only
goto :menu

REM ---------------------------------------------------------------- measure
:analyse
cls
echo   Builds the numeric snapshot for a ticker, then checks that every
echo   number in the report traces back to it.
echo.
set "TICKER="
set /p "TICKER=  Ticker (e.g. NVDA), or blank to go back: "
if defined TICKER set "TICKER=%TICKER:"=%"
if not defined TICKER goto :menu
echo.
"%TR_PYTHON%" tools\snapshot.py %TICKER% --out reports\%TICKER%.snapshot.json
echo.
if errorlevel 1 goto :analyse_failed
echo   Snapshot written to reports\%TICKER%.snapshot.json
echo.
pause
goto :menu

:analyse_failed
echo   The snapshot failed, so nothing was written. Check the ticker and
echo   that this machine has a network connection.
echo.
pause
goto :menu

:tests
cls
echo   Running the toolkit tests.
echo.
"%TR_PYTHON%" -m pytest tests\ -q
echo.
if errorlevel 1 echo   SOMETHING FAILED above. Do not run a live session on a red suite.
if not errorlevel 1 echo   All green.
echo.
pause
goto :menu

:logs
cls
if not exist "%TR_ROOT%logs" goto :no_logs
echo   Opening the logs folder.
start "" "%TR_ROOT%logs"
"%SYS32%\ping.exe" -n 2 127.0.0.1 >nul 2>&1
goto :menu

:no_logs
echo   There is no logs folder yet. It is created by the first session.
echo.
pause
goto :menu

REM ----------------------------------------------------------- tradingview
REM There is no TradingView API. It exposes no endpoint for reading your
REM account, your charts or your trades, so nothing here "connects" to it --
REM and any tool that claims to is either scraping a session cookie or making
REM the data up. The two integrations below are the two TradingView actually
REM supports, and both are already in this toolkit.

:tvimport
cls
echo   Import a TradingView strategy export.
echo.
echo   Where to get the file:
echo       Strategy Tester  ^>  List of Trades  ^>  the export icon
echo       Broker-connected accounts: Trading Panel ^> History ^> export
echo.
echo   WHY THIS IS THE USEFUL ONE. tools/trade_stats.py normalises MT5 and
echo   TradingView into ONE schema, so an imported strategy is measured by
echo   exactly the statistics the live record is measured by -- R-multiple,
echo   date-clustered t, per-symbol breakdown. Two strategies quoted in two
echo   dialects of "win rate" cannot be compared; these can.
echo.
set "TVCSV="
set /p "TVCSV=  Path to the CSV, or blank to go back: "
if defined TVCSV set "TVCSV=%TVCSV:"=%"
if not defined TVCSV goto :menu
if not exist "%TVCSV%" goto :tv_nofile
echo.
"%TR_PYTHON%" tools\tv_import.py "%TVCSV%"
echo.
if errorlevel 1 echo   The import failed. Run it again with --inspect to see which columns were detected.
pause
goto :menu

:tv_nofile
echo.
echo   No file at: %TVCSV%
echo   Drag the CSV onto this window to paste its path exactly.
echo.
pause
goto :menu

:tvalerts
cls
echo   Summarising the TradingView alerts received so far.
echo.
"%TR_PYTHON%" tools\tv_webhook.py --show
echo.
if errorlevel 1 echo   No alert log yet. Start the receiver ^(option 12^) and point a TradingView alert at it.
pause
goto :menu

:tvserve
cls
echo   ================================================================
echo     TradingView alert receiver
echo   ================================================================
echo.
echo     RECORDS ONLY. It holds no broker credentials and places no
echo     order. An alert receiver wired straight to an execution
echo     endpoint is how a research tool becomes an unattended bot --
echo     a different thing, with different failure modes, that should
echo     be a deliberate decision rather than a default.
echo.
echo     It listens on 127.0.0.1 by default, which TradingView cannot
echo     reach from the internet. To receive real alerts you need to
echo     expose it deliberately; read TRADINGVIEW_ARCHITECTURE.md first.
echo.
echo     Ctrl+C stops it.
echo.
set "TVSEC="
set /p "TVSEC=  Shared secret (blank to go back): "
if defined TVSEC set "TVSEC=%TVSEC:"=%"
if not defined TVSEC goto :menu
cls
"%TR_PYTHON%" tools\tv_webhook.py --secret "%TVSEC%"
echo.
pause
goto :menu

:preflight
cls
echo   Harness preflight - read-only, sends nothing, changes nothing.
echo.
echo   Answers the forward-looking question that --dry-run cannot: it
echo   refuses early on a weekend deadline and then reports nothing else,
echo   so on a Friday it tells you the one thing you already knew. This
echo   runs every check regardless and says which evening DOES work.
echo.
if not defined TR_MT5 goto :need_mt5
"%TR_PYTHON%" tools\preflight.py --until-hour 6
if errorlevel 1 echo   Fix the FAIL lines above before starting a session.
pause
goto :menu

REM ---------------------------------------------------------------- search
REM HOW TO READ ANY OF THESE. A candidate is evidence only if it clears the
REM Bonferroni threshold, beats the permutation null -- a shuffle of its OWN
REM signals, so it trades as often and pays the same spread -- AND holds out
REM of sample. Eight searches across five universes have now run and not one
REM candidate has cleared all three. A big in-sample number is the normal
REM appearance of nothing: the highest t this project ever produced was 6.59,
REM and it went to -1.82 out of sample.

:svolume
cls
echo   Volume search - crypto D1, seven Binance pairs, 3000 bars.
echo.
echo   The last untested input. Two of the six candidates IGNORE volume and
echo   are there as controls: the return leg alone is momentum, which is
echo   already refuted, so without them a positive could not be credited to
echo   volume rather than to momentum.
echo.
echo   Needs a network connection. Takes about two minutes.
echo.
"%TR_PYTHON%" tools\volume_search.py --symbols "BTC/USDT,ETH/USDT,BNB/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,SOL/USDT" --bars 3000 --out reports\volume_search_d1.json
echo.
if errorlevel 1 goto :search_failed
echo   Written to reports\volume_search_d1.json
echo.
echo   Last run: the weighting made it WORSE than the control at both
echo   quantiles, and the permutation null beat every real candidate.
echo.
pause
goto :menu

:ssuper
cls
echo   Supertrend search - FX majors, H1.
echo.
echo   The only concrete strategy in three public trading repositories.
echo   Supertrend is not a rename of a searched family: its band ratchets,
echo   so the flip level carries state from every bar since the last flip.
echo.
echo   Needs MetaTrader 5 open for the bars. Takes about three minutes.
echo.
if not defined TR_MT5 goto :need_mt5
"%TR_PYTHON%" tools\supertrend_search.py --timeframe H1 --out reports\supertrend_search_h1.json
echo.
if errorlevel 1 goto :search_failed
echo   Written to reports\supertrend_search_h1.json
echo.
echo   Last run: significantly NEGATIVE - the source rule scores -2.27 in
echo   sample and -4.07 out, and loses in all seven pairs.
echo.
pause
goto :menu

:srules
cls
echo   Rule search - the 41-candidate baseline across ten families.
echo.
echo   RSI, MA crosses, Donchian, Bollinger, momentum and the inverse of
echo   each. This is the run every later search is compared against.
echo.
echo   Needs MetaTrader 5 open. Takes about five minutes.
echo.
REM Writes to _rerun, NOT over reports\rule_search.json. That file is the
REM canonical baseline and CLAUDE.md quotes its figures by name throughout --
REM best in-sample t 1.29, the null's 0.74, zero of 41 clearing 1.96 out of
REM sample. A menu option that silently replaced it would make every one of
REM those citations refer to a different run than the one they were written
REM about, and nothing would flag it.
if not defined TR_MT5 goto :need_mt5
"%TR_PYTHON%" tools\rule_search.py --timeframe H1 --out reports\rule_search_rerun.json
echo.
if errorlevel 1 goto :search_failed
echo   Written to reports\rule_search_rerun.json
echo.
echo   Deliberately NOT written over reports\rule_search.json - that is the
echo   baseline CLAUDE.md quotes by number. Compare the two rather than
echo   replacing one with the other.
echo.
pause
goto :menu

:scost
cls
echo   Cost hurdle - the win rate the spread demands before any strategy.
echo.
echo   This is the question that comes BEFORE "does the rule work", and the
echo   answer is a property of the broker and the bracket rather than of any
echo   strategy, so it is computed exactly rather than searched for.
echo.
if not defined TR_MT5 goto :need_mt5
"%TR_PYTHON%" tools\cost_hurdle.py
echo.
if errorlevel 1 goto :search_failed
pause
goto :menu

:search_failed
echo.
echo   The search did not finish, so no report was written. The usual causes
echo   are no network (the crypto search fetches from an exchange) or
echo   MetaTrader 5 being closed (every FX search reads its bars).
echo.
pause
goto :menu

REM ----------------------------------------------------------------- errors
:need_mt5
echo.
echo   MetaTrader5 or psutil is not installed in:
echo       %TR_PYTHON%
echo.
echo   Install both into THAT interpreter and try again:
echo       "%TR_PYTHON%" -m pip install MetaTrader5 psutil
echo.
echo   psutil is the one worth checking twice: without it a session cannot
echo   see another already running on this machine and proceeds UNGUARDED,
echo   and two harvest loops on one account double the risk.
echo.
pause
goto :menu

:no_python
echo.
echo   NO USABLE PYTHON FOUND. Nothing was run.
echo.
echo   Every interpreter checked is missing at least one of numpy, pandas
echo   or requests. Install them into one of them and run this again:
echo.
echo       "%%LOCALAPPDATA%%\Programs\Python\Python314\python.exe" -m pip install -r requirements.txt
echo.
echo   Or create the project's own environment, which is what the tests and
echo   every figure in reports\ were produced under:
echo.
echo       python -m venv .venv
echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause
exit /b 1

:sleeptest
cls
echo   ================================================================
echo     SLEEP TEST - an overnight run that sends NO ORDERS.
echo   ================================================================
echo.
echo     It runs the full loop until 06:00 and holds the machine awake,
echo     but passes no --live, so nothing reaches the venue and there is
echo     no book to strand. That is also why it may start while the
echo     market is shut: it opens nothing a weekend close could catch.
echo.
echo     What it proves: the host stayed awake for the whole session.
echo     Read the pass log afterwards - a GAP in it is the failure.
echo.
echo     NOTE: while AC sleep is set to never, the machine will not try
echo     to sleep at all, so a clean run proves the session survives but
echo     NOT that the power request works. Lower the AC sleep timeout
echo     first if it is the fix itself you want to test.
echo.
if not defined TR_MT5 goto :need_mt5
"%TR_PYTHON%" tools\preflight.py --paper --until-hour 6
echo.
set "OK="
set /p "OK=  Type YES to start the no-order run, anything else to go back: "
if defined OK set "OK=%OK:"=%"
if /i not "%OK%"=="YES" goto :menu
cls
"%TR_PYTHON%" tools\run_overnight.py --until-hour 6 --paper
echo.
pause
goto :menu

:no_input
echo.
echo   No input received three times, so this is not a console. Exiting
echo   rather than spinning. Double-click the file to use the menu.
echo.
endlocal
exit /b 0

:done
endlocal
exit /b 0
