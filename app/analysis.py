"""Basic analysis layer — yield curve, trend (moving average), volatility.

These are the foundational analyses that pi uses as evidence for investment
assistance. All operate on daily data.

- **Yield curve**: term spread (long - short) to diagnose the economic phase.
- **Trend**: moving averages + current position vs MA (overbought/oversold).
- **Volatility**: realized volatility (std of returns), rolling, max drawdown.
"""
from __future__ import annotations

import numpy as np

from app import normalize
from app.collectors import indicators
from app.timeutil import kst_today


def _latest_if_recent(
    points: list[dict] | None, max_age_days: int, today=None
) -> dict | None:
    """The latest observation, unless it is older than the allowance.

    ``today`` exists so a replay can freeze the clock. A freshness gate that
    reads the real wall clock would call every past reading stale and quietly
    empty the verdict it was asked to reproduce.
    """
    if not points:
        return None
    from datetime import date

    point = points[-1]
    try:
        now = today or kst_today()
        if (now - date.fromisoformat(point["date"])).days > max_age_days:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return point


# --------------------------------------------------------------------------
# Yield curve
# --------------------------------------------------------------------------
# Tenors we hold for each sovereign curve, shortest first.  A curve is only
# meaningful when its points share an observation date, so the builder aligns
# on the newest date every requested tenor actually has.
CURVE_TENORS = {
    "kr": (
        (1.0, "kr_treasury_1y", "1년"),
        (2.0, "kr_treasury_2y", "2년"),
        (3.0, "kr_treasury_3y", "3년"),
        (5.0, "kr_treasury_5y", "5년"),
        (10.0, "kr_treasury_10y", "10년"),
        (30.0, "kr_treasury_30y", "30년"),
    ),
    "us": (
        (0.25, "us_3m", "3개월"),
        (1.0, "us_1y", "1년"),
        (2.0, "us_2y", "2년"),
        (5.0, "us_5y", "5년"),
        (10.0, "us_10y", "10년"),
        (30.0, "us_30y", "30년"),
    ),
}


def yield_curve(series: dict[str, list[dict]], country: str) -> dict:
    """Build an aligned sovereign curve plus a comparison a month earlier.

    Mixing tenors observed on different days would draw a curve that never
    existed, so every point comes from one date: the newest on which all
    requested tenors reported.
    """
    tenors = CURVE_TENORS.get(country)
    if not tenors:
        return {"error": f"unknown curve: {country}"}
    by_key = {
        key: {point["date"]: point["value"] for point in series.get(key, [])}
        for _, key, _ in tenors
    }
    available = [set(values) for values in by_key.values() if values]
    if len(available) < 2:
        return {"error": "not enough tenors collected for a curve"}
    common = sorted(set.intersection(*available))
    if not common:
        return {"error": "tenors share no observation date"}
    latest = common[-1]
    # A month of trading days back, or the oldest date we can align on.
    previous = common[max(0, len(common) - 22)] if len(common) > 1 else None

    def read(day: str) -> list[dict]:
        return [
            {
                "tenor_years": years,
                "label": label,
                "key": key,
                "value": by_key[key][day],
            }
            for years, key, label in tenors if day in by_key[key]
        ]

    points = read(latest)
    comparison = read(previous) if previous and previous != latest else []
    spreads = {}
    values = {item["key"]: item["value"] for item in points}
    for name, long_key, short_key in (
        ("10y_2y", f"{country}_treasury_10y", f"{country}_treasury_2y"),
        ("10y_3y", f"{country}_treasury_10y", f"{country}_treasury_3y"),
        ("30y_10y", f"{country}_treasury_30y", f"{country}_treasury_10y"),
        ("us_10y_2y", "us_10y", "us_2y"),
        ("us_30y_10y", "us_30y", "us_10y"),
    ):
        if long_key in values and short_key in values:
            spreads[name] = round(values[long_key] - values[short_key], 3)
    inverted = [
        (points[index]["label"], points[index + 1]["label"])
        for index in range(len(points) - 1)
        if points[index + 1]["value"] < points[index]["value"]
    ]
    return {
        "country": country,
        "as_of": latest,
        "compared_to": previous if comparison else None,
        "points": points,
        "comparison": comparison,
        "spreads": spreads,
        "inverted_segments": inverted,
        "observations": len(common),
        "method": "모든 만기가 함께 관측된 가장 최근 날짜로 정렬한 곡선입니다.",
    }


def term_spread(short: list[dict], long: list[dict]) -> dict | None:
    """Long-term minus short-term yield (e.g. 10y - 2y)."""
    if not short or not long:
        return None
    short_by_date = {point["date"]: point["value"] for point in short}
    long_by_date = {point["date"]: point["value"] for point in long}
    common_dates = sorted(short_by_date.keys() & long_by_date.keys())
    if not common_dates:
        return None
    aligned_date = common_dates[-1]
    s = short_by_date[aligned_date]
    l = long_by_date[aligned_date]
    return {
        "short": s,
        "long": l,
        "spread": round(l - s, 3),
        "inverted": l < s,  # inversion: short > long
        "date": aligned_date,
    }


# --------------------------------------------------------------------------
# Trend (moving averages)
# --------------------------------------------------------------------------
def moving_average(points: list[dict], window: int) -> float | None:
    """Simple moving average of the last `window` values."""
    if len(points) < window:
        return None
    vals = [p["value"] for p in points[-window:]]
    return round(float(np.mean(vals)), 2)


def _py(value):
    """Convert numpy scalar to python native (for JSON serialization)."""
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return value


def trend_analysis(points: list[dict], windows: list[int] | None = None) -> dict:
    """Analyze trend: current price vs moving averages, golden/dead cross."""
    if not points:
        return {"error": "no data"}
    windows = windows or [20, 60, 200]
    current = points[-1]["value"]
    result = {"current": _py(current), "date": points[-1]["date"], "mas": {}, "signals": []}
    for w in windows:
        ma = moving_average(points, w)
        result["mas"][w] = ma
        if ma is not None:
            above = current >= ma
            result["signals"].append({
                "window": w,
                "ma": ma,
                "above": _py(above),
                "pct_from_ma": round((current - ma) / ma * 100, 2),
            })
    # golden/dead cross: short MA crosses above/below long MA
    if len(points) >= 60:
        short_prev = moving_average(points[:-1], 20)
        long_prev = moving_average(points[:-1], 60)
        short_now = result["mas"].get(20)
        long_now = result["mas"].get(60)
        if short_prev and long_prev and short_now and long_now:
            if short_prev <= long_prev and short_now > long_now:
                result["cross"] = "golden"  # 단기 상향 돌파
            elif short_prev >= long_prev and short_now < long_now:
                result["cross"] = "dead"  # 단기 하향 돌파
    return result





# --------------------------------------------------------------------------
# Volatility
# --------------------------------------------------------------------------
def realized_volatility(points: list[dict], window: int = 20) -> dict:
    """Realized volatility = std of daily returns over a window (annualized %)."""
    if len(points) < window + 1:
        return {"error": "not enough data"}
    vals = np.array([p["value"] for p in points], dtype=float)
    rets = np.diff(vals) / vals[:-1]
    recent = rets[-window:]
    vol = float(np.std(recent, ddof=1)) * np.sqrt(252) * 100  # annualized %
    # full-period volatility for comparison
    full_vol = float(np.std(rets, ddof=1)) * np.sqrt(252) * 100
    return {
        "recent_vol_annualized": round(vol, 2),
        "full_vol_annualized": round(full_vol, 2),
        "window": window,
        "elevated": bool(vol > full_vol * 1.3),  # recent vol much higher than usual
    }


def max_drawdown(points: list[dict]) -> dict:
    """Maximum drawdown (largest peak-to-trough decline)."""
    if len(points) < 2:
        return {"error": "not enough data"}
    vals = np.array([p["value"] for p in points], dtype=float)
    peak = vals[0]
    max_dd = 0.0
    running_peak_date = points[0]["date"]
    max_dd_peak_date = points[0]["date"]
    trough_date = points[0]["date"]
    for i, v in enumerate(vals):
        if v > peak:
            peak = v
            running_peak_date = points[i]["date"]
        dd = (v - peak) / peak
        if dd < max_dd:
            max_dd = dd
            max_dd_peak_date = running_peak_date
            trough_date = points[i]["date"]
    return {
        "max_drawdown_pct": round(float(max_dd * 100), 2),
        "peak_date": max_dd_peak_date,
        "trough_date": trough_date,
    }


def full_analysis(points: list[dict]) -> dict:
    """Combine trend + volatility + drawdown for a single series."""
    result = {"points": len(points)}
    result.update(trend_analysis(points))
    result["volatility"] = realized_volatility(points)
    result["max_drawdown"] = max_drawdown(points)
    return result


# --------------------------------------------------------------------------
# Technical indicators
# --------------------------------------------------------------------------
def rsi(points: list[dict], period: int = 14) -> float | None:
    """Relative Strength Index — overbought (>70) / oversold (<30)."""
    if len(points) < period + 1:
        return None
    vals = np.array([p["value"] for p in points], dtype=float)
    deltas = np.diff(vals)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    # Wilder smoothing, which is the convention used by most charting tools.
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(float(100 - 100 / (1 + rs)), 2)


def macd(points: list[dict]) -> dict | None:
    """MACD (12,26,9) — trend momentum. Returns latest MACD, signal, histogram."""
    if len(points) < 35:
        return None
    vals = np.array([p["value"] for p in points], dtype=float)

    def ema(data, span):
        # exponential moving average
        out = np.zeros_like(data)
        out[0] = data[0]
        alpha = 2 / (span + 1)
        for i in range(1, len(data)):
            out[i] = alpha * data[i] + (1 - alpha) * out[i - 1]
        return out

    ema12 = ema(vals, 12)
    ema26 = ema(vals, 26)
    macd_line = ema12 - ema26
    signal = ema(macd_line, 9)
    hist = macd_line - signal
    return {
        "macd": round(float(macd_line[-1]), 3),
        "signal": round(float(signal[-1]), 3),
        "histogram": round(float(hist[-1]), 3),
        "bullish": bool(macd_line[-1] > signal[-1]),  # MACD above signal
    }


def bollinger(points: list[dict], window: int = 20, num_std: float = 2.0) -> dict | None:
    """Bollinger Bands — volatility-based support/resistance."""
    if len(points) < window:
        return None
    vals = np.array([p["value"] for p in points], dtype=float)
    recent = vals[-window:]
    mid = recent.mean()
    sd = recent.std()
    upper = mid + num_std * sd
    lower = mid - num_std * sd
    current = vals[-1]
    # %B: where price sits within the band
    pct_b = (current - lower) / (upper - lower) if upper != lower else 0.5
    return {
        "upper": round(float(upper), 2),
        "middle": round(float(mid), 2),
        "lower": round(float(lower), 2),
        "pct_b": round(float(pct_b), 2),
        "current": round(float(current), 2),
        "touching_upper": bool(current >= upper),  # overbought
        "touching_lower": bool(current <= lower),  # oversold
    }


# --------------------------------------------------------------------------
# Risk metrics
# --------------------------------------------------------------------------
def sharpe_ratio(points: list[dict], risk_free: float = 0.0) -> dict | None:
    """Sharpe ratio — return per unit of risk (annualized)."""
    if len(points) < 30:
        return None
    vals = np.array([p["value"] for p in points], dtype=float)
    rets = np.diff(vals) / vals[:-1]
    mean = rets.mean()
    sd = rets.std(ddof=1)
    if sd == 0:
        return None
    # annualize: mean*252, sd*sqrt(252), subtract risk-free (annualized)
    sharpe = (mean * 252 - risk_free) / (sd * np.sqrt(252))
    return {
        "sharpe_annualized": round(float(sharpe), 2),
        "annual_return_pct": round(float(mean * 252 * 100), 2),
        "annual_vol_pct": round(float(sd * np.sqrt(252) * 100), 2),
        "risk_free_annual": risk_free,
    }


def value_at_risk(points: list[dict], confidence: float = 0.95) -> dict | None:
    """Historical one-day loss quantile and expected shortfall."""
    if len(points) < 30:
        return None
    vals = np.array([p["value"] for p in points], dtype=float)
    rets = np.diff(vals) / vals[:-1]
    # historical VaR: the (1-confidence) quantile of returns
    cutoff = float(np.percentile(rets, (1 - confidence) * 100))
    var = max(0.0, -cutoff)
    tail = rets[rets <= cutoff]
    expected_shortfall = max(0.0, -float(tail.mean())) if len(tail) else var
    return {
        "confidence": confidence,
        "var_1day_pct": round(float(var * 100), 2),
        "var_1day_index_points": round(float(var * vals[-1]), 2),
        "expected_shortfall_1day_pct": round(expected_shortfall * 100, 2),
        "method": "historical",
    }


# --------------------------------------------------------------------------
# Market regime (risk-on / risk-off)
# --------------------------------------------------------------------------
def market_regime(
    vix: list[dict] | None,
    spread: list[dict] | None,
    sp500: list[dict] | None,
    today=None,
) -> dict:
    """Classify market regime using VIX, credit spread, and equity trend.

    Risk-on: low VIX, narrow spread, equity above MA.
    Risk-off: high VIX, wide spread, equity below MA.
    """
    scores = []
    reasons = []

    # VIX: <18 risk-on, >25 risk-off
    vix_point = _latest_if_recent(vix, 7, today)
    if vix_point:
        v = vix_point["value"]
        if v < 18:
            scores.append(1); reasons.append(f"VIX 낮음 ({v})")
        elif v > 25:
            scores.append(-1); reasons.append(f"VIX 높음 ({v})")
        else:
            scores.append(0); reasons.append(f"VIX 중립 ({v})")

    # credit spread (IG): <1% risk-on, >1.5% risk-off
    spread_point = _latest_if_recent(spread, 7, today)
    if spread_point:
        s = spread_point["value"]
        if s < 1.0:
            scores.append(1); reasons.append(f"스프레드 좁음 ({s})")
        elif s > 1.5:
            scores.append(-1); reasons.append(f"스프레드 넓음 ({s})")
        else:
            scores.append(0); reasons.append(f"스프레드 중립 ({s})")

    # S&P 500 vs 200-day MA
    sp_point = _latest_if_recent(sp500, 7, today)
    if sp_point and sp500 and len(sp500) >= 200:
        ma200 = np.mean([p["value"] for p in sp500[-200:]])
        current = sp500[-1]["value"]
        if current >= ma200:
            scores.append(1); reasons.append("S&P 200일선 위")
        else:
            scores.append(-1); reasons.append("S&P 200일선 아래")

    if not scores:
        return {
            "regime": "unknown", "score": 0, "reasons": reasons,
            "as_of": {"vix": None, "credit_spread": None, "sp500": None},
            "method": "rule_based_v1",
        }
    total = sum(scores)
    if total >= 2:
        regime = "risk_on"
    elif total <= -2:
        regime = "risk_off"
    else:
        regime = "neutral"
    return {
        "regime": regime,
        "score": total,
        "reasons": reasons,
        "as_of": {
            "vix": vix_point["date"] if vix_point else None,
            "credit_spread": spread_point["date"] if spread_point else None,
            "sp500": sp_point["date"] if sp_point else None,
        },
        "method": "rule_based_v1",
    }


# --------------------------------------------------------------------------
# Korea market regime (percentile-based)
# --------------------------------------------------------------------------
# The US classifier above reads VIX, the US IG spread, and the S&P 200-day
# average.  All three are American, so on a Korean money-market dashboard it
# answers a question nobody asked: it called 2026-08 "risk_on" while KOSPI sat
# 24.8% below its June peak at 80% annualised volatility.  This one is built
# from Korean inputs and differs in two ways that the data forced.
#
# It scores each input against *its own* recent distribution rather than an
# absolute threshold.  A 0.688%p AA- corporate spread looks narrow next to the
# US IG rule of thumb (<1.0%), but it sits at the 87th percentile of its own
# last 250 sessions — wide, not narrow.  A fixed cut would have read the sign
# backwards.
#
# It also scores trend and drawdown separately, both from KOSPI.  That double
# weight is deliberate: a market above its 200-day average *and* deep below its
# 52-week high is a bounce inside a crash, and making the two components
# disagree is precisely what surfaces that state instead of averaging it away.
# The window policy lives with the series, not here; see app/normalize.py.
KR_PERCENTILE_WINDOW = normalize.TRAILING_WINDOW
KR_DRAWDOWN_WINDOW = 252
KR_RISK_ON_PERCENTILE = 80.0
KR_RISK_OFF_PERCENTILE = 20.0
KR_MIN_ACTIVE_COMPONENTS = 3
KR_MAX_AGE_DAYS = 7

# Following the sentiment gauge: a component whose history is too short to
# normalise is excluded and reported, never filled with a plausible guess.
KR_MIN_HISTORY = {
    "volatility": 50,    # VKOSPI against its own distribution
    "credit": 60,        # a spread against its own recent distribution
    "funding": 60,
    "trend": 200,        # the moving average it is measured against
    # a drawdown series needs its own trailing window before it starts, and
    # then enough of itself to be a distribution
    "drawdown": KR_DRAWDOWN_WINDOW + 250,
}


def _risk_on_percentile(value: float, history: list[float], *, invert: bool) -> float:
    """Where ``value`` sits in ``history``, oriented so 100 reads as risk-on.

    Delegates to :mod:`app.normalize`, which is the single definition. This
    classifier and the sentiment gauge scored the same VKOSPI differently for
    as long as each carried its own copy.
    """
    return normalize.percentile(value, history, invert=invert)


def _percentile_component(
    key: str,
    label: str,
    series: list[dict] | None,
    *,
    today=None,
    invert: bool,
    unit: str,
    note: str,
    window: int | None = KR_PERCENTILE_WINDOW,
) -> dict:
    """Score one series against its own distribution.

    ``window`` of None means the full history. Which is right depends on the
    series: a spread's normal level drifts with the rate cycle, so a trailing
    year is the honest baseline. Implied volatility does not drift — it mean
    reverts — so a trailing year that happens to contain a crash lets the
    crash define what normal means and scores itself as unremarkable. That is
    the same trap the drawdown component avoids, for the same reason.
    """
    minimum = KR_MIN_HISTORY[key]
    if not series or len(series) < minimum:
        return {
            "key": key, "label": label, "score": None,
            "reason": f"{note} 이력이 {minimum}거래일 이상 쌓이면 활성화됩니다 "
                      f"(현재 {len(series or [])})",
        }
    point = _latest_if_recent(series, KR_MAX_AGE_DAYS, today)
    if point is None:
        return {
            "key": key, "label": label, "score": None,
            "reason": f"{note} 최신 관측이 {KR_MAX_AGE_DAYS}일보다 오래돼 제외했습니다",
        }
    values = [item["value"] for item in series]
    scope = values if window is None else values[-window:]
    percentile = _risk_on_percentile(point["value"], scope, invert=invert)
    span = "전체" if window is None else "최근"
    return {
        "key": key,
        "label": label,
        "score": _cut(percentile),
        "percentile": percentile,
        "value": point["value"],
        "as_of": point["date"],
        "detail": f"{note} {point['value']}{unit}, {span} {len(scope)}"
                  f"거래일 분포에서 위험선호 방향 {percentile:.0f}점",
    }


def _cut(percentile: float) -> int:
    if percentile >= KR_RISK_ON_PERCENTILE:
        return 1
    if percentile <= KR_RISK_OFF_PERCENTILE:
        return -1
    return 0


def _kospi_trend(kospi: list[dict] | None, today=None) -> dict:
    minimum = KR_MIN_HISTORY["trend"]
    if not kospi or len(kospi) < minimum:
        return {
            "key": "trend", "label": "주가 추세", "score": None,
            "reason": f"코스피 이력이 {minimum}거래일 이상 쌓이면 활성화됩니다 "
                      f"(현재 {len(kospi or [])})",
        }
    point = _latest_if_recent(kospi, KR_MAX_AGE_DAYS, today)
    if point is None:
        return {
            "key": "trend", "label": "주가 추세", "score": None,
            "reason": f"코스피 최신 관측이 {KR_MAX_AGE_DAYS}일보다 오래돼 제외했습니다",
        }
    values = [item["value"] for item in kospi]
    average = float(np.mean(values[-minimum:]))
    deviation = (values[-1] - average) / average * 100
    return {
        "key": "trend",
        "label": "주가 추세",
        "score": 1 if values[-1] >= average else -1,
        "value": round(deviation, 2),
        "as_of": point["date"],
        "detail": f"코스피가 {minimum}일 이동평균 대비 {deviation:+.1f}%",
    }


def _kospi_drawdown(kospi: list[dict] | None, today=None) -> dict:
    """Score the distance below the 52-week high against 20 years of the same.

    The window here is the full history, not the trailing 250 sessions the
    spreads use: a spread's normal level drifts with the rate cycle, but a
    drawdown is already a ratio, and a one-year baseline would let the current
    crash define what "normal" means and score itself as unremarkable.
    """
    minimum = KR_MIN_HISTORY["drawdown"]
    if not kospi or len(kospi) < minimum:
        return {
            "key": "drawdown", "label": "고점 대비 낙폭", "score": None,
            "reason": f"코스피 이력이 {minimum}거래일 이상 쌓이면 활성화됩니다 "
                      f"(현재 {len(kospi or [])})",
        }
    point = _latest_if_recent(kospi, KR_MAX_AGE_DAYS, today)
    if point is None:
        return {
            "key": "drawdown", "label": "고점 대비 낙폭", "score": None,
            "reason": f"코스피 최신 관측이 {KR_MAX_AGE_DAYS}일보다 오래돼 제외했습니다",
        }
    values = [item["value"] for item in kospi]
    window = KR_DRAWDOWN_WINDOW
    history = [
        values[index] / max(values[index - window:index + 1]) - 1
        for index in range(window, len(values))
    ]
    current = history[-1]
    percentile = _risk_on_percentile(current, history, invert=False)
    return {
        "key": "drawdown",
        "label": "고점 대비 낙폭",
        "score": _cut(percentile),
        "percentile": percentile,
        "value": round(current * 100, 2),
        "as_of": point["date"],
        "detail": f"코스피가 52주 고점 대비 {current * 100:+.1f}%, "
                  f"20년 낙폭 분포에서 위험선호 방향 {percentile:.0f}점",
    }


def korea_regime(
    vkospi: list[dict] | None,
    credit_spread: list[dict] | None,
    funding_spread: list[dict] | None,
    kospi: list[dict] | None,
    today=None,
) -> dict:
    """Classify Korean conditions from Korean inputs, by percentile.

    Each component votes -1/0/+1 and the verdict uses the *ratio* of the net
    vote to the components that actually reported. A raw sum would make this
    classifier more trigger-happy than the three-input US one simply because
    it has more inputs; the ratio keeps the bar at the same place and lets a
    component drop out without silently loosening the verdict.
    """
    components = [
        _percentile_component(
            "volatility", "변동성", vkospi, invert=True, unit="",
            note="VKOSPI", window=normalize.window_for("kr_vkospi"), today=today,
        ),
        _percentile_component(
            "credit", "회사채 신용", credit_spread, invert=True, unit="%p",
            note="회사채 AA- 3년 − 국고채 3년 스프레드", today=today,
        ),
        _percentile_component(
            "funding", "단기 자금시장", funding_spread, invert=True, unit="%p",
            note="CP 91일 − CD 91일 스프레드", today=today,
        ),
        _kospi_trend(kospi, today),
        _kospi_drawdown(kospi, today),
    ]
    active = [item for item in components if item.get("score") is not None]
    pending = [
        {"key": item["key"], "label": item["label"], "reason": item["reason"]}
        for item in components if item.get("score") is None
    ]
    total = sum(int(item["score"]) for item in active)
    if len(active) < KR_MIN_ACTIVE_COMPONENTS:
        regime = "unknown"
        ratio = None
    else:
        ratio = total / len(active)
        if ratio >= 0.5:
            regime = "risk_on"
        elif ratio <= -0.5:
            regime = "risk_off"
        else:
            regime = "neutral"
    return {
        "regime": regime,
        "score": total,
        "ratio": None if ratio is None else round(ratio, 2),
        "reasons": [item["detail"] for item in active],
        "components": active,
        "pending": pending,
        "component_count": len(active),
        "component_total": len(components),
        "as_of": max(
            (item["as_of"] for item in active if item.get("as_of")), default=None
        ),
        "method": "kr_percentile_rule_v1",
        "method_note": (
            f"각 입력을 자체 분포에서 위험선호 방향 백분위로 환산해 "
            f"{KR_RISK_ON_PERCENTILE:.0f}점 이상 +1, "
            f"{KR_RISK_OFF_PERCENTILE:.0f}점 이하 −1로 채점합니다. "
            f"스프레드는 최근 {KR_PERCENTILE_WINDOW}거래일, 변동성과 낙폭은 "
            f"전체 이력을 분포로 씁니다. 스프레드의 정상 수준은 금리 사이클과 "
            f"함께 이동하지만 변동성과 낙폭은 평균회귀하므로, 1년 창을 쓰면 "
            f"지금의 위기가 스스로의 기준선이 됩니다. 이력이 부족한 구성요소는 추정하지 않고 pending에 "
            f"사유를 남기며, 활성 구성요소가 "
            f"{KR_MIN_ACTIVE_COMPONENTS}개 미만이면 판정하지 않습니다."
        ),
    }
