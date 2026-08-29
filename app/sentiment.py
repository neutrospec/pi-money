"""A Korean market sentiment gauge built from this project's own collection.

CNN's Fear & Greed index is a useful shape but a closed one: the weights are
unpublished, the inputs are American, and a number nobody can reproduce is a
poor basis for a decision.  This module keeps the shape — several independent
readings of risk appetite, each normalised to a 0-100 scale and averaged —
while making every input, threshold, and omission explicit.

Two rules follow from that.  A component whose history is too short to
normalise is *excluded and reported*, never filled with a plausible guess; and
the composite states how many components it actually stood on, so a reader can
discount it.
"""
from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

from app import db, market_metrics
from app.timeutil import kst_today


# Bands follow the familiar five-step reading. They are descriptive labels for
# a position on the scale, not signals.
BANDS = (
    (25, "extreme_fear", "극단적 공포"),
    (45, "fear", "공포"),
    (55, "neutral", "중립"),
    (75, "greed", "탐욕"),
    (101, "extreme_greed", "극단적 탐욕"),
)

MIN_HISTORY = {
    "momentum": 125,        # the moving average it is measured against
    "strength": 20,         # a high/low window worth the name
    "breadth": 1,           # one session is a reading
    "volatility": 50,       # VKOSPI against its own trend
    "put_call": 1,          # the ratio has a conventional neutral point
    "safe_haven": 20,       # a 20-session relative return
    "credit": 60,           # a spread against its own recent distribution
}


def _scale(value: float, low: float, high: float) -> float:
    """Map a value onto 0-100, clipped. ``low`` reads as fear, ``high`` greed."""
    if high == low:
        return 50.0
    position = (value - low) / (high - low) * 100
    return round(max(0.0, min(100.0, position)), 1)


def _percentile_score(value: float, history: list[float], *, invert: bool) -> float:
    """Score a value by where it sits in its own recent distribution.

    Using the series' own history rather than a fixed threshold keeps the
    reading meaningful for a series whose normal level drifts.
    """
    if not history:
        return 50.0
    below = sum(1 for item in history if item < value)
    percentile = below / len(history) * 100
    return round(100 - percentile if invert else percentile, 1)


def _series(key: str) -> list[dict]:
    return db.get_indicator_points(key)


def _momentum() -> dict | None:
    points = db.get_index_points("^KS11")
    if len(points) < MIN_HISTORY["momentum"]:
        return None
    values = [point["value"] for point in points]
    average = sum(values[-125:]) / 125
    deviation = (values[-1] - average) / average * 100
    return {
        "key": "momentum",
        "label": "주가 모멘텀",
        "score": _scale(deviation, -10.0, 10.0),
        "detail": f"코스피가 125일 이동평균 대비 {deviation:+.1f}%",
        "as_of": points[-1]["date"],
        "method": "(종가 − 125일 이동평균) / 125일 이동평균, ±10%를 양 끝으로",
    }


@lru_cache(maxsize=1)
def _breadth_snapshot() -> dict:
    """Read the KRX breadth snapshot once per gauge build.

    Two components need it and each full scan costs several hundred
    milliseconds against a table of two hundred thousand rows.  The cache is
    cleared at the start of every :func:`gauge` call so a page never serves a
    reading older than the request that asked for it.
    """
    return market_metrics.krx_breadth_snapshot()


def _strength() -> dict | None:
    """New highs against new lows across every KRX-listed stock.

    The breadth snapshot already counts these while computing its 20-day
    statistics, so this reads them rather than scanning the table again.
    """
    snapshot = _breadth_snapshot()
    highs = lows = eligible = 0
    for market in snapshot["markets"]:
        if market["status"] != "ok":
            continue
        history = market.get("history_20d") or {}
        highs += history.get("new_highs") or 0
        lows += history.get("new_lows") or 0
        eligible += history.get("eligible_issues") or 0
    if eligible < 100:
        return None
    total = highs + lows
    ratio = 0.5 if total == 0 else highs / total
    return {
        "key": "strength",
        "label": "주가 강도",
        "score": _scale(ratio, 0.0, 1.0),
        "detail": (
            f"20일 신고가 {highs}종목 vs 신저가 {lows}종목 ({eligible}종목 대상)"
        ),
        "as_of": snapshot["as_of"],
        "method": "신고가 / (신고가 + 신저가), 20거래일 기준",
    }


def _breadth() -> dict | None:
    """Turnover flowing into rising shares against falling ones."""
    snapshot = _breadth_snapshot()
    up = down = 0.0
    for market in snapshot["markets"]:
        if market["status"] != "ok":
            continue
        up += market.get("up_turnover") or 0.0
        down += market.get("down_turnover") or 0.0
    if up + down <= 0:
        return None
    share = up / (up + down)
    return {
        "key": "breadth",
        "label": "시장 폭",
        "score": _scale(share, 0.3, 0.7),
        "detail": f"상승 종목 거래대금 비중 {share * 100:.1f}%",
        "as_of": snapshot["as_of"],
        "method": "상승 거래대금 / (상승 + 하락 거래대금), 30~70%를 양 끝으로",
    }


def _volatility() -> dict | None:
    """VKOSPI against its own recent level, not an absolute threshold.

    The KRX collector lifts VKOSPI out of the 320-row derivative index table
    into its own series, so this reads a named indicator rather than scanning
    a bulk table for a row matched by name.
    """
    points = _series("kr_vkospi")
    if len(points) < MIN_HISTORY["volatility"]:
        return None
    values = [point["value"] for point in points]
    return {
        "key": "volatility",
        "label": "변동성 (VKOSPI)",
        "score": _percentile_score(values[-1], values[-250:], invert=True),
        "detail": f"VKOSPI {values[-1]:.2f}",
        "as_of": points[-1]["date"],
        "method": "최근 250거래일 분포상 백분위, 높을수록 공포",
    }


def _put_call() -> dict | None:
    """Index option positioning. One is the conventional balance point."""
    points = _series("kr_put_call_volume")
    if not points:
        return None
    ratio = points[-1]["value"]
    return {
        "key": "put_call",
        "label": "풋/콜 비율",
        "score": _scale(ratio, 1.4, 0.6),
        "detail": f"코스피200 옵션 풋/콜 거래량 {ratio:.3f}",
        "as_of": points[-1]["date"],
        "method": "1.0을 중립으로, 0.6(탐욕)~1.4(공포)를 양 끝으로",
    }


def _safe_haven() -> dict | None:
    """Equities against bonds over the same recent window."""
    equity = db.get_index_points("^KS11")
    yields = _series("kr_treasury_10y")
    if len(equity) < 25 or len(yields) < 25:
        return None
    equity_return = (equity[-1]["value"] / equity[-21]["value"] - 1) * 100
    # A falling long yield is a bond rally; the sign is inverted so both legs
    # read as returns.
    bond_return = (yields[-21]["value"] - yields[-1]["value"]) * 10
    spread = equity_return - bond_return
    return {
        "key": "safe_haven",
        "label": "안전자산 수요",
        "score": _scale(spread, -10.0, 10.0),
        "detail": f"20거래일 주식 {equity_return:+.1f}% vs 국고채 10년 대용 {bond_return:+.1f}%",
        "as_of": min(equity[-1]["date"], yields[-1]["date"]),
        "method": "코스피 20일 수익률 − 국고채 10년 금리하락폭×10",
    }


def _credit() -> dict | None:
    """The BBB- spread against its own recent distribution."""
    low = {point["date"]: point["value"] for point in _series("kr_corp_bond_bbb")}
    base = {point["date"]: point["value"] for point in _series("kr_treasury_3y")}
    days = sorted(low.keys() & base.keys())
    if len(days) < MIN_HISTORY["credit"]:
        return None
    spreads = [low[day] - base[day] for day in days]
    return {
        "key": "credit",
        "label": "신용 수요",
        "score": _percentile_score(spreads[-1], spreads[-250:], invert=True),
        "detail": f"회사채 BBB− 스프레드 {spreads[-1]:.2f}%p",
        "as_of": days[-1],
        "method": "최근 250거래일 분포상 백분위, 확대될수록 공포",
    }


COMPONENTS = (
    _momentum, _strength, _breadth, _volatility, _put_call, _safe_haven, _credit,
)

PENDING_REASON = {
    "strength": "KRX 전 종목 이력이 20거래일 이상 쌓이면 활성화됩니다",
    "volatility": "VKOSPI 이력이 50거래일 이상 쌓이면 활성화됩니다",
    "momentum": "코스피 125거래일 이력이 필요합니다",
    "breadth": "KRX 승인 데이터가 필요합니다",
    "put_call": "KRX 옵션 데이터가 필요합니다",
    "safe_haven": "코스피와 국고채 10년 이력이 필요합니다",
    "credit": "회사채 BBB− 이력이 60거래일 이상 필요합니다",
}


def band(score: float) -> tuple[str, str]:
    for threshold, key, label in BANDS:
        if score < threshold:
            return key, label
    return BANDS[-1][1], BANDS[-1][2]


def gauge() -> dict:
    """Compose the gauge from whichever components can be measured today."""
    _breadth_snapshot.cache_clear()
    live, pending = [], []
    for build in COMPONENTS:
        try:
            component = build()
        except Exception as exc:  # one input must not sink the gauge
            component = None
            name = build.__name__.strip("_")
            pending.append({
                "key": name,
                "reason": f"{type(exc).__name__}: {exc}",
            })
            continue
        if component:
            live.append(component)
        else:
            name = build.__name__.strip("_")
            pending.append({
                "key": name,
                "reason": PENDING_REASON.get(name, "입력 데이터가 아직 부족합니다"),
            })
    if not live:
        return {
            "status": "unavailable",
            "reason": "측정 가능한 구성요소가 없습니다",
            "components": [],
            "pending": pending,
            "cached": True,
        }
    score = round(sum(item["score"] for item in live) / len(live), 1)
    key, label = band(score)
    return {
        "status": "ok",
        "score": score,
        "band": key,
        "band_label": label,
        "components": live,
        "pending": pending,
        "component_count": len(live),
        "component_total": len(COMPONENTS),
        "as_of": max(item["as_of"] for item in live if item["as_of"]),
        "method": (
            "구성요소별 0~100 점수의 단순 평균입니다. 가중치를 두지 않은 것은 "
            "가중치를 정당화할 표본이 아직 없기 때문입니다. 이력이 부족한 "
            "구성요소는 추정하지 않고 제외하며 pending에 사유를 남깁니다."
        ),
        "warning": (
            "임계값 기반 참고 지표이며 매수·매도 신호가 아닙니다. "
            "CNN Fear & Greed와 산식·입력이 다르므로 수치를 직접 비교하지 마세요."
        ),
        "cached": True,
    }
