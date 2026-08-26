"""Basic analysis layer — yield curve, trend (moving average), volatility.

These are the foundational analyses that pi uses as evidence for investment
assistance. All operate on daily data.

- **Yield curve**: term spread (long - short) to diagnose the economic phase.
- **Trend**: moving averages + current position vs MA (overbought/oversold).
- **Volatility**: realized volatility (std of returns), rolling, max drawdown.
"""
from __future__ import annotations

import numpy as np

from app.timeutil import kst_today


def _latest_if_recent(points: list[dict] | None, max_age_days: int) -> dict | None:
    if not points:
        return None
    from datetime import date

    point = points[-1]
    try:
        if (kst_today() - date.fromisoformat(point["date"])).days > max_age_days:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return point


# --------------------------------------------------------------------------
# Yield curve
# --------------------------------------------------------------------------
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
) -> dict:
    """Classify market regime using VIX, credit spread, and equity trend.

    Risk-on: low VIX, narrow spread, equity above MA.
    Risk-off: high VIX, wide spread, equity below MA.
    """
    scores = []
    reasons = []

    # VIX: <18 risk-on, >25 risk-off
    vix_point = _latest_if_recent(vix, 7)
    if vix_point:
        v = vix_point["value"]
        if v < 18:
            scores.append(1); reasons.append(f"VIX 낮음 ({v})")
        elif v > 25:
            scores.append(-1); reasons.append(f"VIX 높음 ({v})")
        else:
            scores.append(0); reasons.append(f"VIX 중립 ({v})")

    # credit spread (IG): <1% risk-on, >1.5% risk-off
    spread_point = _latest_if_recent(spread, 7)
    if spread_point:
        s = spread_point["value"]
        if s < 1.0:
            scores.append(1); reasons.append(f"스프레드 좁음 ({s})")
        elif s > 1.5:
            scores.append(-1); reasons.append(f"스프레드 넓음 ({s})")
        else:
            scores.append(0); reasons.append(f"스프레드 중립 ({s})")

    # S&P 500 vs 200-day MA
    sp_point = _latest_if_recent(sp500, 7)
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
