"""Statistics for drift and calibration.

Every function here answers one question and refuses to answer it on a sample
too small to mean anything. That refusal is the point: a drift score computed
over twenty observations is noise wearing a number's clothes, and this
repository has spent its whole history distinguishing the two.

`benjamini_hochberg` is deliberately the same procedure as
`tools/patterns.benjamini_hochberg`. It is restated here rather than imported
because the backend does not depend on the research toolkit's packages, and
`tests/test_monitoring.py` imports both and asserts they agree on the same
input, so the two cannot drift apart silently.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

# Under about 100 observations a win rate or a distribution statistic is
# dominated by luck. The research tooling says so in `sample_size_assessment`
# and the live record says so in `track_record.py`; monitoring inherits it.
MIN_SAMPLE = 100

# PSI bands in common use. They are CONVENTION, not a measurement: no study in
# this repository established them, and they are reported as thresholds that
# were chosen rather than found.
PSI_WARN = 0.10
PSI_SERIOUS = 0.25
PSI_BAND_SOURCE = "industry convention, not measured here"


def benjamini_hochberg(pvalues: Sequence[float], q: float = 0.05) -> list[bool]:
    """Which hypotheses survive FDR control at level q.

    Monitoring k features is k hypotheses, and at k=20 one expects a
    'significant' feature per run with nothing happening. Must match
    `tools/patterns.benjamini_hochberg` exactly.
    """
    n = len(pvalues)
    if not n:
        return []
    order = sorted(range(n), key=lambda i: pvalues[i])
    survive = [False] * n
    threshold_rank = -1
    for rank, idx in enumerate(order, start=1):
        if pvalues[idx] <= q * rank / n:
            threshold_rank = rank
    for rank, idx in enumerate(order, start=1):
        if rank <= threshold_rank:
            survive[idx] = True
    return survive


def normal_sf(z: float) -> float:
    """Two-sided p-value from a z score. Matches `patterns._norm_sf`."""
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2.0))))


def quantile_edges(values: Sequence[float], bins: int) -> list[float]:
    """Bin edges from the reference distribution's own quantiles.

    Equal-width bins over an unknown distribution put most of the mass in one
    bucket and report no drift whatever happens. Quantile edges come from the
    reference sample, so each reference bucket starts equally populated.
    """
    if bins < 2:
        raise ValueError("need at least two bins")
    ordered = sorted(values)
    if not ordered:
        return []
    edges = [-math.inf]
    for i in range(1, bins):
        pos = i * (len(ordered) - 1) / bins
        low = ordered[int(math.floor(pos))]
        high = ordered[int(math.ceil(pos))]
        edges.append(low + (high - low) * (pos - math.floor(pos)))
    edges.append(math.inf)
    # Ties can collapse edges; a degenerate reference gets fewer bins rather
    # than a divide-by-zero.
    deduped = [edges[0]]
    for edge in edges[1:]:
        if edge > deduped[-1]:
            deduped.append(edge)
    return deduped


def _bucket_shares(values: Sequence[float], edges: Sequence[float]) -> list[float]:
    counts = [0] * (len(edges) - 1)
    for value in values:
        for i in range(len(edges) - 1):
            if edges[i] < value <= edges[i + 1]:
                counts[i] += 1
                break
    total = sum(counts) or 1
    return [c / total for c in counts]


def population_stability_index(
    reference: Sequence[float], current: Sequence[float], bins: int = 10
) -> float:
    """PSI between two samples, using the reference's quantile edges.

    Zero-share buckets are floored rather than dropped: dropping them hides
    exactly the case PSI exists to catch, a region of the reference the
    current sample has abandoned entirely.
    """
    edges = quantile_edges(reference, bins)
    if len(edges) < 3:
        return 0.0
    floor = 1.0 / (max(len(reference), len(current)) * 10)
    ref_shares = _bucket_shares(reference, edges)
    cur_shares = _bucket_shares(current, edges)
    total = 0.0
    for r, c in zip(ref_shares, cur_shares, strict=True):
        r = max(r, floor)
        c = max(c, floor)
        total += (c - r) * math.log(c / r)
    return total


def ks_statistic(a: Sequence[float], b: Sequence[float]) -> tuple[float, float]:
    """Two-sample Kolmogorov-Smirnov D and its asymptotic p-value."""
    if not a or not b:
        return 0.0, 1.0
    xs = sorted(set(a) | set(b))
    sa, sb = sorted(a), sorted(b)
    na, nb = len(sa), len(sb)

    def cdf(sample: list[float], n: int, x: float) -> float:
        low, high = 0, n
        while low < high:
            mid = (low + high) // 2
            if sample[mid] <= x:
                low = mid + 1
            else:
                high = mid
        return low / n

    d = max(abs(cdf(sa, na, x) - cdf(sb, nb, x)) for x in xs)
    ne = na * nb / (na + nb)
    lam = (math.sqrt(ne) + 0.12 + 0.11 / math.sqrt(ne)) * d
    p = 2.0 * sum((-1) ** (k - 1) * math.exp(-2.0 * k * k * lam * lam) for k in range(1, 101))
    return d, min(max(p, 0.0), 1.0)


def categorical_psi(reference: Sequence[str], current: Sequence[str]) -> float:
    """PSI over labels, for regimes and other categories."""
    labels = sorted(set(reference) | set(current))
    if not labels:
        return 0.0
    ref_counts, cur_counts = Counter(reference), Counter(current)
    ref_total = sum(ref_counts.values()) or 1
    cur_total = sum(cur_counts.values()) or 1
    floor = 1.0 / (max(ref_total, cur_total) * 10)
    total = 0.0
    for label in labels:
        r = max(ref_counts[label] / ref_total, floor)
        c = max(cur_counts[label] / cur_total, floor)
        total += (c - r) * math.log(c / r)
    return total


def brier_score(probabilities: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error of probabilistic predictions. Lower is better."""
    if len(probabilities) != len(outcomes):
        raise ValueError("probabilities and outcomes differ in length")
    if not probabilities:
        return 0.0
    return sum((p - o) ** 2 for p, o in zip(probabilities, outcomes, strict=True)) / len(
        probabilities
    )


def reliability_bins(
    probabilities: Sequence[float], outcomes: Sequence[int], bins: int = 10
) -> list[dict[str, float]]:
    """Predicted probability against observed frequency, per bin."""
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for p, o in zip(probabilities, outcomes, strict=True):
        index = min(int(p * bins), bins - 1)
        buckets[index].append((p, o))
    out = []
    for i, bucket in enumerate(buckets):
        if not bucket:
            continue
        out.append(
            {
                "bin": i / bins,
                "n": len(bucket),
                "mean_predicted": sum(p for p, _ in bucket) / len(bucket),
                "observed": sum(o for _, o in bucket) / len(bucket),
            }
        )
    return out


def expected_calibration_error(
    probabilities: Sequence[float], outcomes: Sequence[int], bins: int = 10
) -> float:
    """Sample-weighted gap between predicted probability and observed rate."""
    rows = reliability_bins(probabilities, outcomes, bins)
    n = len(probabilities)
    if not n or not rows:
        return 0.0
    return sum(r["n"] * abs(r["mean_predicted"] - r["observed"]) for r in rows) / n


def welch_t(a: Sequence[float], b: Sequence[float]) -> tuple[float, float]:
    """Welch t and two-sided p for two means. Returns (0, 1) when undefined."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return 0.0, 1.0
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    denom = math.sqrt(va / na + vb / nb)
    if denom == 0:
        return 0.0, 1.0
    t = (ma - mb) / denom
    return t, normal_sf(t)
