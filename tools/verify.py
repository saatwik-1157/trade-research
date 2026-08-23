"""Check that every number in a written report traces back to the snapshot.

The rule this enforces: an agent may only state a figure that the data layer
actually produced. Instructing a model to "cite or omit" is a request; this
makes it a test. Run it on any generated report and it lists the numbers that
appear in the prose but not in the data, and for the ones that do check out, it
names the field they came from.

A flagged number is not automatically wrong - it may be sourced from a filing
or a news article the agent cited inline. It is a number that has to be
justified rather than assumed.

What this does and does not establish: it checks that a figure *exists* in the
computed data, not that it means what the surrounding sentence claims. Matching
is on value alone, so a figure can occasionally be attributed to an unrelated
field that happens to hold the same number. It catches invented figures, which
is the failure that matters; it is not a substitute for reading the report.

Matching is deliberately narrow. An earlier version accepted every value
multiplied by 100 and divided by 10^3 through 10^12, which produced an allowed
set so large that invented figures matched by coincidence. Instead, the way a
number is *written* determines which interpretations are legal: a percent sign
permits the /100 reading, a B/M/K/T suffix permits that one scale, and a bare
number must match the data as-is.

Usage:
    python tools/verify.py reports/NVDA.report.md reports/NVDA.snapshot.json
    python tools/verify.py reports/NVDA.report.md reports/NVDA.snapshot.json --strict
"""
from __future__ import annotations

import argparse
import json
import re
import sys

# Report prose contains typographic characters (minus signs, dashes, quotes)
# that a cp1252 console cannot encode. Without this the printer raises
# UnicodeEncodeError mid-listing, and because the exception escapes before the
# strict exit code is returned, the shell sees 0 and a failing gate reads as a
# passing one. A gate that crashes must never look like a gate that passed.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):  # pragma: no cover - non-reconfigurable stream
    pass

# Numbers with optional currency, thousands separators, percent, or a
# magnitude suffix: $215.9B, 71.1%, 1,234.56, -0.20
# Multi-letter magnitude suffixes must be listed before the single-letter class:
# "bn" written as [BMKTbmkt]\b never matches, because b followed by n is not a
# word boundary, so every "$4.434bn" in a report silently parsed as bare 4.434
# and failed to match its own source figure.
# The comma group takes + rather than *: with *, the first alternative matches
# "202" out of "2022" and stops, and the truncated value then slips past the
# year filter and is reported as an unverified claim. Requiring at least one
# comma group means genuinely grouped numbers take the first branch and plain
# runs of digits take the second, whole.
NUMBER_RE = re.compile(
    r"(?<![\w.])(-?\$?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\$?\d+(?:\.\d+)?)"
    r"\s*(bn|mn|tn|bln|[%BMKTbmkt]\b|%)?",
    re.IGNORECASE,
)

SUFFIX_SCALE = {
    "k": 1e3,
    "m": 1e6, "mn": 1e6,
    "b": 1e9, "bn": 1e9, "bln": 1e9,
    "t": 1e12, "tn": 1e12,
}

# Contexts where a bare number is structural rather than a claim about the
# company: indicator periods, dates, fiscal labels.
IGNORE_CONTEXT = re.compile(
    r"(?:RSI|SMA|EMA|MACD|ATR|ADX|BB|Bollinger|MA)\s*[\(\-]?\s*\d+"
    r"|\b\d+\s*[-\s]?(?:day|week|month|year|bar|period|session)s?\b"
    r"|\b(?:Q[1-4]|FY|H[12])\s*\d{2,4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    # Fibonacci ratio labels name a level, they are not claims about the company
    r"|\b0\.(?:236|382|5|500|618|786)\b",
    re.IGNORECASE,
)

URL_RE = re.compile(r"https?://\S+|\]\([^)]*\)|`[^`]*`")


def collect(obj, out: dict[float, list[str]], path: str = "") -> dict[float, list[str]]:
    """Map every numeric leaf in the snapshot to the field paths that hold it."""
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        try:
            out.setdefault(abs(float(obj)), []).append(path or "root")
        except (TypeError, ValueError, OverflowError):
            pass
    elif isinstance(obj, dict):
        for k, v in obj.items():
            collect(v, out, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            collect(v, out, f"{path}[{i}]")
    elif isinstance(obj, str):
        # Only strings that are entirely a number count as data. Scraping digits
        # out of arbitrary text would admit "10-K", accession numbers and dates
        # as evidence, which lets a fabricated figure match a form type.
        try:
            out.setdefault(abs(float(obj.strip())), []).append(path or "root")
        except ValueError:
            pass
    return out


def candidates(value: float, suffix: str) -> list[tuple[float, float]]:
    """Legal readings of a written number, each with its scale factor.

    How the number is written decides which interpretations are legal, and the
    scale is carried alongside so the rounding tolerance can be expressed in
    the same units as the stored value.
    """
    s = suffix.lower().strip()
    if s == "%":
        # "71.1%" may be stored as 71.1 or as the ratio 0.711
        return [(abs(value), 1.0), (abs(value) / 100.0, 0.01)]
    if s in SUFFIX_SCALE:
        return [(abs(value) * SUFFIX_SCALE[s], SUFFIX_SCALE[s])]
    return [(abs(value), 1.0)]


def written_precision(token: str) -> float:
    """Half of the last written digit: how much rounding the text permits.

    A report that says "10.0%" against stored 10.045% is correctly rounded, not
    fabricated, so the tolerance has to follow the precision the author used
    rather than a single global percentage.
    """
    cleaned = token.replace("$", "").replace(",", "").strip()
    decimals = len(cleaned.split(".")[1]) if "." in cleaned else 0
    return 0.5 * (10.0 ** -decimals)


def find_match(value: float, suffix: str, token: str, table: dict[float, list[str]], rel_tol: float):
    """Return the field path backing this number, or None."""
    half_ulp = written_precision(token)
    best, best_gap = None, None
    for cand, scale in candidates(value, suffix):
        tol_from_rounding = half_ulp * scale
        for known, paths in table.items():
            tol = max(rel_tol * abs(known), tol_from_rounding, 1e-9)
            gap = abs(cand - known)
            if gap <= tol and (best_gap is None or gap < best_gap):
                # Closest match, so attribution names the field the author most
                # plausibly read rather than whichever one hashed first.
                best, best_gap = paths[0], gap
    return best


HAS_SOURCE_RE = re.compile(r"https?://[^\s)\]]+")


def extract_claims(text: str) -> list[tuple[str, float, str, str, bool]]:
    """Numeric claims as (token, value, suffix, source line, line_is_sourced).

    A line carrying a source URL satisfies the "cite or omit" rule even when the
    figure is absent from the snapshot: a sentiment agent quoting a filing or an
    article is doing exactly what it was asked to do. Without this distinction
    the strict gate fails on correctly-sourced external facts, which would
    either block every report or train the writer to strip citations - the
    opposite of the intent.
    """
    claims = []
    for raw_line in text.splitlines():
        sourced = bool(HAS_SOURCE_RE.search(raw_line))
        line = URL_RE.sub(" ", raw_line)  # links and code spans are not claims
        if not line.strip() or line.strip().startswith(("#", ">")):
            continue
        # Suppress only numbers that are *part of* a structural phrase. Matching
        # on a surrounding window instead would silently excuse any figure that
        # happened to sit near an indicator name or a fiscal-year label.
        ignore_spans = [m.span() for m in IGNORE_CONTEXT.finditer(line)]
        for m in NUMBER_RE.finditer(line):
            start, end = m.span(1)
            if any(s <= start and end <= e for s, e in ignore_spans):
                continue
            token, suffix = m.group(1), (m.group(2) or "").strip()
            try:
                value = float(token.replace("$", "").replace(",", ""))
            except ValueError:
                continue
            # Bare small integers and bare years are almost always structural
            if not suffix and float(value).is_integer():
                if abs(value) < 32 or 1900 <= value <= 2100:
                    continue
            claims.append((m.group(0).strip(), value, suffix, raw_line.strip(), sourced))
    return claims


def verify(report_path: str, snapshot_path: str, rel_tol: float = 0.002) -> dict:
    with open(report_path, encoding="utf-8") as fh:
        text = fh.read()
    with open(snapshot_path, encoding="utf-8") as fh:
        snapshot = json.load(fh)

    table = collect(snapshot, {})
    claims = extract_claims(text)

    verified, sourced_external, unverified = [], [], []
    for token, value, suffix, line, is_sourced in claims:
        path = find_match(value, suffix, token, table, rel_tol)
        entry = {"token": token, "parsed_value": value, "line": line[:150]}
        if path:
            entry["source_field"] = path
            verified.append(entry)
        elif is_sourced:
            # Not in the data, but the line carries a citation the reader can follow.
            sourced_external.append(entry)
        else:
            unverified.append(entry)

    return {
        "report": report_path,
        "snapshot": snapshot_path,
        "numeric_claims": len(claims),
        "verified": len(verified),
        "externally_sourced": len(sourced_external),
        "externally_sourced_detail": sourced_external,
        "unverified": unverified,
        "verified_detail": verified,
        "coverage": round((len(verified) + len(sourced_external)) / len(claims), 4) if claims else None,
        "coverage_from_snapshot": round(len(verified) / len(claims), 4) if claims else None,
        "tolerance": rel_tol,
        "note": "Unverified numbers are not necessarily wrong - they may be sourced inline "
                "from a filing or article. They are the numbers that need a citation.",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Check every number in a report against its snapshot.")
    ap.add_argument("report")
    ap.add_argument("snapshot")
    ap.add_argument("--tolerance", type=float, default=0.002, help="relative tolerance (default 0.2%%)")
    ap.add_argument("--strict", action="store_true", help="exit non-zero if anything is unverified")
    ap.add_argument("--show-verified", action="store_true", help="list each verified number and its field")
    ap.add_argument("--json", action="store_true", help="emit the full result as JSON")
    args = ap.parse_args()

    result = verify(args.report, args.snapshot, args.tolerance)

    if args.json:
        print(json.dumps(result, indent=2))
        return 1 if (args.strict and result["unverified"]) else 0

    print(f"\n{result['numeric_claims']} numeric claims: "
          f"{result['verified']} trace to the snapshot, "
          f"{result['externally_sourced']} carry an inline source, "
          f"{len(result['unverified'])} unaccounted for   (coverage {result['coverage']})\n")

    if args.show_verified:
        for v in result["verified_detail"]:
            print(f"  ok  {v['token']:>12}  <-  {v['source_field']}")
        print()

    if result["unverified"]:
        print("Unverified figures - each needs an inline source or removal:\n")
        for u in result["unverified"]:
            print(f"  {u['token']:>12}   {u['line']}")
    else:
        print("  Every figure in the report traces to computed data.")
    print()

    return 1 if (args.strict and result["unverified"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
