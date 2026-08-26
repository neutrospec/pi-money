"""Cross-market return correlation with explicit sample diagnostics."""
from __future__ import annotations

import math

import numpy as np


MIN_OBSERVATIONS = 20


def _return_map(points: list[dict]) -> dict[str, float]:
    """Map each observation date to its own previous-session simple return."""
    ordered = sorted(points, key=lambda point: point["date"])
    out: dict[str, float] = {}
    previous: float | None = None
    for point in ordered:
        try:
            value = float(point["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if not np.isfinite(value) or value <= 0:
            previous = None
            continue
        if previous is not None:
            out[point["date"]] = (value / previous - 1.0) * 100
        previous = value
    return out


def _daily_returns(points: list[dict]) -> tuple[list, np.ndarray]:
    mapped = _return_map(points)
    dates = sorted(mapped)
    return dates, np.array([mapped[day] for day in dates], dtype=float)


def _align(series: dict[str, list]) -> tuple[list, dict[str, np.ndarray]]:
    """Align precomputed daily returns on common dates."""
    if not series:
        return [], {}
    returns = {name: _return_map(points) for name, points in series.items()}
    common: set[str] | None = None
    for mapped in returns.values():
        dates = set(mapped)
        common = dates if common is None else common & dates
    dates = sorted(common or set())
    aligned = {
        name: np.array([mapped[day] for day in dates], dtype=float)
        for name, mapped in returns.items()
    }
    return dates, aligned


def _align_pair(a: list[dict], b: list[dict]) -> tuple[list[str], np.ndarray, np.ndarray]:
    dates, aligned = _align({"a": a, "b": b})
    return dates, aligned.get("a", np.array([])), aligned.get("b", np.array([]))


def _corr(a: np.ndarray, b: np.ndarray) -> float | None:
    if len(a) < MIN_OBSERVATIONS or len(b) != len(a):
        return None
    if np.std(a) == 0 or np.std(b) == 0:
        return None
    value = float(np.corrcoef(a, b)[0, 1])
    return value if np.isfinite(value) else None


def correlation_matrix(series: dict[str, list]) -> dict:
    """Pairwise Pearson matrix; each pair uses its own common-date sample."""
    names = list(series)
    if len(names) < 2:
        return {"error": "need at least two index series", "names": names, "matrix": []}
    size = len(names)
    matrix: list[list[float | None]] = [[None] * size for _ in range(size)]
    observations: list[list[int]] = [[0] * size for _ in range(size)]
    maps = {name: _return_map(series[name]) for name in names}
    for i, left in enumerate(names):
        for j in range(i, size):
            right = names[j]
            common = sorted(maps[left].keys() & maps[right].keys())
            a = np.array([maps[left][day] for day in common], dtype=float)
            b = np.array([maps[right][day] for day in common], dtype=float)
            value = 1.0 if i == j and len(common) >= MIN_OBSERVATIONS else _corr(a, b)
            rounded = round(value, 3) if value is not None else None
            matrix[i][j] = matrix[j][i] = rounded
            observations[i][j] = observations[j][i] = len(common)
    return {
        "names": names,
        "matrix": matrix,
        "observations": observations,
        "method": "pairwise Pearson correlation of daily simple returns",
    }


def rolling_correlation(a: list[dict], b: list[dict], window: int = 60) -> dict:
    if not 20 <= window <= 252:
        return {"error": "window must be between 20 and 252"}
    dates, left, right = _align_pair(a, b)
    if len(left) < window:
        return {"error": f"need at least {window} common return observations"}
    out_dates, out_values = [], []
    for end in range(window, len(left) + 1):
        value = _corr(left[end - window:end], right[end - window:end])
        if value is None:
            continue
        out_dates.append(dates[end - 1])
        out_values.append(round(value, 3))
    return {
        "dates": out_dates,
        "values": out_values,
        "window": window,
        "observations": len(left),
        "method": "rolling Pearson correlation of common-date daily returns",
    }


def _approx_p_value(correlation: float, observations: int) -> float:
    """Two-sided Fisher-z normal approximation for H0: correlation=0."""
    if observations <= 3 or abs(correlation) >= 1:
        return 0.0 if abs(correlation) >= 1 else 1.0
    z = math.atanh(correlation) * math.sqrt(observations - 3)
    return math.erfc(abs(z) / math.sqrt(2))


def lead_lag(a: list[dict], b: list[dict], max_lag: int = 10) -> dict:
    if not 0 <= max_lag <= 20:
        return {"error": "max_lag must be between 0 and 20"}
    _, left, right = _align_pair(a, b)
    if len(left) < MIN_OBSERVATIONS + max_lag:
        return {"error": "not enough common return observations"}
    lags, correlations, p_values, counts = [], [], [], []
    for lag in range(-max_lag, max_lag + 1):
        if lag > 0:
            x, y = left[:-lag], right[lag:]
        elif lag < 0:
            x, y = left[-lag:], right[:lag]
        else:
            x, y = left, right
        value = _corr(x, y)
        lags.append(lag)
        counts.append(len(x))
        correlations.append(round(value, 3) if value is not None else None)
        p_values.append(round(_approx_p_value(value, len(x)), 4) if value is not None else None)
    candidates = [
        (abs(value), index) for index, value in enumerate(correlations)
        if value is not None
    ]
    best = None
    if candidates:
        _, index = max(candidates)
        best = {
            "lag": lags[index],
            "correlation": correlations[index],
            "p_value": p_values[index],
            "significant_5pct": p_values[index] is not None and p_values[index] < 0.05,
        }
    return {
        "lags": lags,
        "correlations": correlations,
        "p_values": p_values,
        "observations": counts,
        "max_lag": max_lag,
        "best": best,
        "interpretation": (
            "양의 lag은 A의 관측일 수익률이 B보다 해당 공통 session 수만큼 "
            "앞선다는 뜻입니다. 시간대·장 마감 시각 차이가 남아 있으므로 "
            "예측력이나 인과관계의 증거가 아닙니다."
        ),
    }
