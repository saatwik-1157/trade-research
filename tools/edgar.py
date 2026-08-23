"""SEC EDGAR fundamentals via the XBRL company-facts API.

Filed financials, straight from the filings, with the accession number and
filing date attached to every figure. This is the authoritative source for
anything that appears in a 10-K or 10-Q; the provider profile in market.py is a
convenience layer and loses to EDGAR wherever the two disagree.

Two XBRL traps this module exists to avoid, both of which silently produce
numbers that look plausible and are wrong:

1. A 10-K contains quarterly facts as well as annual ones, and both carry
   fp="FY". Filtering on form and fp alone mixes a Q4 revenue figure into an
   annual series. Flow concepts are therefore filtered on period *duration*.
2. The `fy` field is the fiscal year of the filing, not of the fact. One 10-K
   tags three years of comparatives with the same `fy`. Facts are therefore
   keyed on their period end date.

Ratios are only ever computed between figures covering the same period.

The SEC requires a descriptive User-Agent with contact info and rate-limits to
10 requests/second. Set SEC_USER_AGENT in the environment to identify yourself.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone

import requests

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

USER_AGENT = os.environ.get("SEC_USER_AGENT", "trade-research/1.0 (set SEC_USER_AGENT to identify yourself)")
HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
_LAST_CALL = [0.0]
MIN_INTERVAL = 0.12  # stay under the SEC's 10 req/s ceiling

ANNUAL_MIN_DAYS, ANNUAL_MAX_DAYS = 330, 400

# Each metric lists the us-gaap tags we accept, in preference order, plus
# whether it is a flow (measured over a period) or an instant (a balance on a
# date). XBRL tagging varies between filers and changes over time, so a metric
# may be assembled from more than one tag across different years.
CONCEPTS = {
    "revenue": (["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                 "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"], "duration"),
    "gross_profit": (["GrossProfit"], "duration"),
    "operating_income": (["OperatingIncomeLoss"], "duration"),
    "net_income": (["NetIncomeLoss", "ProfitLoss"], "duration"),
    "operating_cash_flow": (["NetCashProvidedByUsedInOperatingActivities",
                             "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"], "duration"),
    "capex": (["PaymentsToAcquirePropertyPlantAndEquipment",
               "PaymentsToAcquireProductiveAssets"], "duration"),
    "rnd_expense": (["ResearchAndDevelopmentExpense"], "duration"),
    "eps_diluted": (["EarningsPerShareDiluted"], "duration"),
    "shares_diluted": (["WeightedAverageNumberOfDilutedSharesOutstanding"], "duration"),
    "assets": (["Assets"], "instant"),
    "liabilities": (["Liabilities"], "instant"),
    "equity": (["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], "instant"),
    "cash": (["CashAndCashEquivalentsAtCarryingValue",
              "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"], "instant"),
    "short_term_investments": (["ShortTermInvestments", "MarketableSecuritiesCurrent",
                                "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                                "OtherShortTermInvestments"], "instant"),
    "total_debt_lt": (["LongTermDebtNoncurrent", "LongTermDebt"], "instant"),
}


def _throttle() -> None:
    elapsed = time.monotonic() - _LAST_CALL[0]
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    _LAST_CALL[0] = time.monotonic()


def _get(url: str, cache_key: str, max_age_s: int = 24 * 3600):
    path = os.path.join(CACHE_DIR, f"{cache_key}.json")
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < max_age_s:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
    _throttle()
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code in (403, 404):
        return None
    resp.raise_for_status()
    data = resp.json()
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass
    return data


def cik_for_ticker(ticker: str) -> str | None:
    """Zero-padded 10-digit CIK, or None for tickers the SEC does not list."""
    data = _get("https://www.sec.gov/files/company_tickers.json", "sec_ticker_map", max_age_s=7 * 24 * 3600)
    if not data:
        return None
    target = ticker.upper().replace("-", "").replace(".", "")
    for row in data.values():
        if str(row.get("ticker", "")).upper().replace("-", "").replace(".", "") == target:
            return str(row["cik_str"]).zfill(10)
    return None


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _annual_facts(cik: str, tags: list[str], kind: str) -> dict[str, dict]:
    """Annual facts for one metric, keyed by period end date.

    Walks the accepted tags in preference order and takes each period end from
    the highest-preference tag that reports it, so a metric whose tag changed
    mid-history (an ASC 606 transition, say) still yields a continuous series.
    Where a period was restated, the most recently filed value wins.
    """
    out: dict[str, dict] = {}
    for tag in tags:
        data = _get(
            f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json",
            f"sec_concept_{cik}_{tag}",
        )
        if not data:
            continue
        for unit_name, rows in (data.get("units") or {}).items():
            for r in rows:
                end, start = r.get("end"), r.get("start")
                if not end or r.get("val") is None:
                    continue
                if kind == "duration":
                    # The fix: an annual figure covers roughly a year. Without
                    # this, Q4 facts from the 10-K land in the annual series.
                    if not start or not (ANNUAL_MIN_DAYS <= _days(start, end) <= ANNUAL_MAX_DAYS):
                        continue
                elif start:
                    continue  # instants have no start date
                if end in out and out[end]["xbrl_tag"] != tag:
                    continue  # a higher-preference tag already supplied this period
                prior = out.get(end)
                if prior and (prior.get("filed") or "") >= (r.get("filed") or ""):
                    continue
                out[end] = {
                    "period_end": end,
                    "period_start": start,
                    "value": r.get("val"),
                    "unit": unit_name,
                    "form": r.get("form"),
                    "fiscal_year": r.get("fy"),
                    "fiscal_period": r.get("fp"),
                    "filed": r.get("filed"),
                    "accession": r.get("accn"),
                    "xbrl_tag": tag,
                }
    return out


def _nearest(facts: dict[str, dict], target_end: str, tol_days: int = 20):
    """Value for the period ending closest to target_end, within tolerance.

    Fiscal period ends drift by a few days under 52/53-week calendars, so an
    exact string match would drop valid alignments.
    """
    if not facts:
        return None
    if target_end in facts:
        return facts[target_end]
    best, best_gap = None, None
    for end, row in facts.items():
        gap = abs(_days(min(end, target_end), max(end, target_end)))
        if gap <= tol_days and (best_gap is None or gap < best_gap):
            best, best_gap = row, gap
    return best


def recent_filings(cik: str, forms=("10-K", "10-Q", "8-K"), limit: int = 10) -> list[dict]:
    """Recent filings with direct URLs, so a report can cite the actual document."""
    data = _get(f"https://data.sec.gov/submissions/CIK{cik}.json", f"sec_submissions_{cik}", max_age_s=12 * 3600)
    if not data:
        return []
    recent = (data.get("filings") or {}).get("recent") or {}
    rows = []
    for i, form in enumerate(recent.get("form", [])):
        if form not in forms:
            continue
        accn = recent["accessionNumber"][i]
        rows.append({
            "form": form,
            "filing_date": recent["filingDate"][i],
            "report_date": (recent.get("reportDate") or [None])[i] if recent.get("reportDate") else None,
            "accession": accn,
            "url": (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{accn.replace('-', '')}/{recent['primaryDocument'][i]}"
            ),
        })
        if len(rows) >= limit:
            break
    return rows


def _ratio(num, den):
    if num is None or not den:
        return None
    return round(num / den, 6)


def fundamentals(ticker: str, years: int = 6) -> dict:
    """Filed annual financials aligned by fiscal period, plus derived ratios."""
    cik = cik_for_ticker(ticker)
    if not cik:
        return {
            "available": False,
            "reason": f"{ticker} is not in the SEC ticker index (foreign issuer, ETF, or delisted)",
        }

    entity = _get(f"https://data.sec.gov/submissions/CIK{cik}.json", f"sec_submissions_{cik}", max_age_s=12 * 3600) or {}
    facts = {name: _annual_facts(cik, tags, kind) for name, (tags, kind) in CONCEPTS.items()}

    if not facts.get("revenue"):
        return {
            "available": False,
            "reason": f"no annual revenue concept found in EDGAR XBRL for CIK {cik}",
            "cik": cik,
        }

    # Fiscal periods are anchored on revenue, the one line every filer reports.
    period_ends = sorted(facts["revenue"], reverse=True)[:years]

    periods = []
    for end in period_ends:
        row = {"period_end": end}
        for name in CONCEPTS:
            fact = _nearest(facts[name], end)
            row[name] = fact["value"] if fact else None
            if fact:
                row.setdefault("_sources", {})[name] = {
                    "xbrl_tag": fact["xbrl_tag"],
                    "form": fact["form"],
                    "filed": fact["filed"],
                    "accession": fact["accession"],
                    "period_end": fact["period_end"],
                }
        periods.append(row)

    latest = periods[0] if periods else {}
    prior = periods[1] if len(periods) > 1 else {}

    rev = latest.get("revenue")
    ocf, capex = latest.get("operating_cash_flow"), latest.get("capex")
    fcf = (ocf - capex) if (ocf is not None and capex is not None) else None
    cash = latest.get("cash")
    sti = latest.get("short_term_investments")
    debt = latest.get("total_debt_lt")
    # A missing short-term investments tag must not silently become zero: that
    # would understate liquidity on every filer who tags marketable securities
    # under a concept we do not recognise.
    liquid = None if cash is None else (cash + sti if sti is not None else cash)
    liquid_basis = (
        "cash + short-term investments" if (cash is not None and sti is not None)
        else "cash only (no recognised short-term investments tag; liquidity understated)"
        if cash is not None else "unavailable"
    )

    rev_series = [(p["period_end"], p["revenue"]) for p in periods if p.get("revenue")]
    rev_cagr = None
    rev_cagr_note = None
    if len(rev_series) >= 2:
        newest, oldest = rev_series[0][1], rev_series[-1][1]
        span = len(rev_series) - 1
        if oldest and newest and oldest > 0 and span:
            rev_cagr = round((newest / oldest) ** (1 / span) - 1, 6)
            # A CAGR measured off a near-zero base describes a company starting
            # production, not a compounding business. Rivian's 4-year figure is
            # 214% purely because FY2021 revenue was $55m. The number is
            # arithmetically correct and substantively meaningless, so it is
            # labelled rather than quietly reported alongside honest ones.
            if oldest < 0.05 * newest:
                rev_cagr_note = (
                    f"Base year revenue ({oldest:,.0f}) is under 5% of the latest "
                    f"({newest:,.0f}); this CAGR reflects a near-zero starting base "
                    "and should not be read as a growth rate."
                )

    derived = {
        "gross_margin": _ratio(latest.get("gross_profit"), rev),
        "operating_margin": _ratio(latest.get("operating_income"), rev),
        "net_margin": _ratio(latest.get("net_income"), rev),
        "rnd_intensity": _ratio(latest.get("rnd_expense"), rev),
        "return_on_equity": _ratio(latest.get("net_income"), latest.get("equity")),
        "return_on_assets": _ratio(latest.get("net_income"), latest.get("assets")),
        "free_cash_flow": fcf,
        "fcf_margin": _ratio(fcf, rev),
        "fcf_conversion": _ratio(fcf, latest.get("net_income")),
        "debt_to_equity": _ratio(debt, latest.get("equity")),
        "liquid_assets": liquid,
        "liquid_assets_basis": liquid_basis,
        "net_cash": (liquid - debt) if (liquid is not None and debt is not None) else None,
        "revenue_yoy": _ratio(rev - prior["revenue"], prior["revenue"]) if (rev and prior.get("revenue")) else None,
        "revenue_cagr": rev_cagr,
        "revenue_cagr_caveat": rev_cagr_note,
        "revenue_cagr_years": max(len(rev_series) - 1, 0),
        "net_income_yoy": (
            _ratio(latest["net_income"] - prior["net_income"], abs(prior["net_income"]))
            if (latest.get("net_income") is not None and prior.get("net_income")) else None
        ),
    }

    # Any ratio built from misaligned periods is a bug, not a finding. Flag the
    # alignment explicitly so a reader can check it rather than trust it.
    alignment = {
        name: (latest.get("_sources", {}).get(name) or {}).get("period_end")
        for name in ("revenue", "gross_profit", "operating_income", "net_income",
                     "equity", "assets", "operating_cash_flow", "capex")
    }

    # This module reads annual periods only, so between fiscal year ends the
    # picture silently ages. For a company burning cash that gap is material -
    # two quarters of burn can sit outside the newest figure here - so the age
    # is reported rather than left for a reader to notice.
    filings = recent_filings(cik)
    newest_report = max(
        (f["report_date"] for f in filings if f.get("report_date")),
        default=None,
    )
    staleness = None
    if latest.get("period_end") and newest_report and newest_report > latest["period_end"]:
        staleness = {
            "annual_period_end": latest["period_end"],
            "most_recent_filed_period": newest_report,
            "days_behind": _days(latest["period_end"], newest_report),
            "note": "Fundamentals here cover the latest full fiscal year. Interim "
                    "results through the most recent filed period are NOT included; "
                    "treat every balance-sheet figure as of the annual period end.",
        }

    return {
        "available": True,
        "cik": cik,
        "entity_name": entity.get("name"),
        "sic_description": entity.get("sicDescription"),
        "fiscal_year_end": entity.get("fiscalYearEnd"),
        "latest_period_end": latest.get("period_end"),
        "staleness": staleness,
        "annual_periods": periods,
        "derived": derived,
        "period_alignment": alignment,
        "derived_note": (
            "Every ratio is computed from figures covering the same fiscal period; "
            "period_alignment lists the period end used for each input, and each "
            "input's XBRL tag, form, filing date and accession number are in "
            "annual_periods[]._sources."
        ),
        "recent_filings": filings,
        "provenance": {
            "source": "SEC EDGAR XBRL company-facts API (data.sec.gov)",
            "authoritative": True,
            "annual_filter": f"flow concepts restricted to periods of {ANNUAL_MIN_DAYS}-{ANNUAL_MAX_DAYS} days",
            "user_agent": USER_AGENT,
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    }
