#!/usr/bin/env bash
# End-to-end demonstration of the trade-research pipeline.
#
#   bash demo.sh            # full run
#   bash demo.sh AMD        # run the snapshot stage on a different ticker
#
# Every command below is the real CLI, not a wrapper. Nothing is pre-baked:
# the ticker is fetched live, the indicators are computed from the price series
# on the spot, and the fabricated figures in the verifier stage are genuinely
# caught rather than announced.

set -u
export PYTHONIOENCODING=utf-8
: "${SEC_USER_AGENT:=trade-research/1.0 demo}"
export SEC_USER_AGENT

TICKER="${1:-AMD}"
cd "$(dirname "$0")" || exit 1

hr() { printf '\n\033[1m%s\033[0m\n%s\n' "$1" "$(printf '=%.0s' $(seq 1 72))"; }

hr "1. INDICATOR MATHS  —  checked against independent calculations"
python tests/test_indicators.py 2>&1 | grep -E "^(SMA|RSI|ATR|MACD|Max|Beta|Swing|Fibonacci|Missing|All|[0-9]+ FAILED)"
echo "  ($(python tests/test_indicators.py 2>&1 | grep -c '^  PASS') checks passed)"

hr "2. DATA LAYER  —  live fetch and compute for $TICKER"
echo "\$ python tools/snapshot.py $TICKER --out reports/$TICKER.snapshot.json"
python tools/snapshot.py "$TICKER" --out "reports/$TICKER.snapshot.json"

python - "$TICKER" <<'PYEOF'
import json, sys
t = sys.argv[1]
s = json.load(open(f"reports/{t}.snapshot.json", encoding="utf-8"))
tech, edg = s["technical"], s.get("edgar", {})

print("\n  Computed from the price series (not from any text):")
for k in ("price", "sma200", "rsi14", "adx14", "atr_pct", "realized_vol_60d",
          "beta_vs_benchmark", "max_drawdown_1y"):
    print(f"    {k:<20} {tech[k]}")

fib = tech.get("fibonacci")
if fib:
    print(f"\n  Fibonacci names its swing (a level without one is meaningless):")
    print(f"    swing high {fib['swing_high']} on {fib['swing_high_date']}")
    print(f"    swing low  {fib['swing_low']} on {fib['swing_low_date']}  [{fib['direction']}]")

if edg.get("available"):
    d = edg["derived"]
    src = edg["annual_periods"][0]["_sources"]["revenue"]
    print(f"\n  Filed financials, period end {edg['latest_period_end']}:")
    print(f"    revenue        {edg['annual_periods'][0]['revenue']:,}")
    print(f"    gross margin   {d['gross_margin']}")
    print(f"    net margin     {d['net_margin']}")
    print(f"    traceable to   {src['form']} {src['accession']} filed {src['filed']}")
    if edg.get("staleness"):
        print(f"    STALE          {edg['staleness']['days_behind']} days behind "
              f"{edg['staleness']['most_recent_filed_period']}")

c = s["scores"]["composite"]
print(f"\n  Composite {c['score']}  (coverage {c['coverage']})")
for k, v in c["component_scores"].items():
    print(f"    {k:<12} {v}")
val = s["scores"]["subscores"]["valuation"]
if val.get("undefined_multiples"):
    print(f"    valuation dropped as undefined: {val['undefined_multiples']}")
if s.get("data_gaps"):
    print(f"\n  Honest gaps ({len(s['data_gaps'])}):")
    for g in s["data_gaps"][:4]:
        print(f"    - {g}")
PYEOF

hr "3. THE GUARDRAIL  —  a report with 13 real figures and 4 planted fakes"
echo "\$ python tools/verify.py tests/sample_report.md reports/NVDA.snapshot.json"
python tools/verify.py tests/sample_report.md reports/NVDA.snapshot.json
python tools/verify.py tests/sample_report.md reports/NVDA.snapshot.json --strict >/dev/null 2>&1
echo "  strict exit code: $?  (non-zero blocks the report from being shown)"

hr "4. A REPORT THAT PASSES  —  the RIVN note written by the five agents"
echo "\$ python tools/verify.py reports/RIVN.report.md reports/RIVN.snapshot.json --strict"
python tools/verify.py reports/RIVN.report.md reports/RIVN.snapshot.json --strict
echo "  strict exit code: $?"

hr "5. DOES THE SCORE PREDICT ANYTHING?"
echo "\$ python tools/backtest.py --universe tools/universe.txt --years 6 --horizon 63"
python tools/backtest.py --universe tools/universe.txt --years 6 --horizon 63 \
  --out reports/backtest_63d.json 2>/dev/null | grep -vE "^Wrote"

hr "6. YOUR OWN TRADES  —  TradingView export"
echo "\$ python tools/tv_import.py tests/tv_list_of_trades.csv --symbol SOLUSDT"
python tools/tv_import.py tests/tv_list_of_trades.csv --symbol SOLUSDT 2>&1 | tail -20

hr "7. YOUR OWN TRADES  —  MetaTrader 5 (live terminal, read-only)"
echo "\$ python tools/mt5_account.py --days 3650"
python tools/mt5_account.py --days 3650 2>&1 | head -8

hr "8. LIVE ALERTS  —  webhook receiver"
PORT=8799
LOG=data/demo_alerts.jsonl
rm -f "$LOG"
python tools/tv_webhook.py --secret "demo-secret" --port "$PORT" --log "$LOG" >/dev/null 2>&1 &
WH=$!
for _ in $(seq 1 25); do curl -s --max-time 1 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; done

printf '  health          : %s\n' "$(curl -s http://127.0.0.1:$PORT/health)"
printf '  wrong secret    : HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:$PORT/ -d '{"secret":"nope","ticker":"X"}')"
printf '  valid alert     : HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:$PORT/ -d '{"secret":"demo-secret","ticker":"RIVN","action":"buy","price":"16.97"}')"
printf '  plain-text alert: HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:$PORT/ --data-raw 'demo-secret SOLUSDT crossed above 160')"
kill "$WH" 2>/dev/null; wait "$WH" 2>/dev/null

echo
echo "  what was written to disk:"
sed 's/^/    /' "$LOG"
if grep -q "demo-secret" "$LOG"; then
  echo "    SECRET LEAKED"
else
  echo "    secret absent from the log in both payload shapes"
fi
rm -f "$LOG"

hr "DONE"
echo "  Nothing in this run placed, modified or cancelled an order."
echo
