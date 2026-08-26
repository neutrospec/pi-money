"""Cache-only assembly of the market situation view.

The dashboard answers one question — what state is the market in right now —
so it reads from the same cache every other interface reads and performs no
provider calls.  Ordering here is editorial: policy and funding first,
because they set the discount rate everything else is priced against, then
risk appetite, then the real economy's own prices.
"""
from __future__ import annotations

from datetime import date, timedelta

from app import analysis, db, market_metrics, sentiment
from app.collectors import indicators, indices
from app.timeutil import kst_today, to_kst


# A tile's direction says what a rise *means*, not whether it is good.  The
# interface needs this to avoid painting a widening credit spread with the
# same colour as a rising index.
RISK = "up_is_risk"        # a rise tightens conditions or signals stress
NEUTRAL = "neutral"        # a rise is neither tightening nor easing per se

HEADLINE_GROUPS = [
    ("한국 자금시장", [
        ("kr_base_rate", NEUTRAL),
        ("kr_kofr", NEUTRAL),
        ("kr_cd_91d", NEUTRAL),
        ("kr_cp_91d", NEUTRAL),
        ("kr_treasury_3y", NEUTRAL),
        ("kr_treasury_10y", NEUTRAL),
        ("kr_corp_bond_3y", NEUTRAL),
        ("kr_usd", RISK),
    ]),
    ("미국 금리·위험", [
        ("us_rate", NEUTRAL),
        ("us_2y", NEUTRAL),
        ("us_10y", NEUTRAL),
        ("us_hy_spread", RISK),
        ("us_vix", RISK),
        ("us_dollar_index", RISK),
    ]),
    ("실물·원자재", [
        ("wti", NEUTRAL),
        ("gold", NEUTRAL),
        ("copper", NEUTRAL),
        ("kr_cpi", NEUTRAL),
        ("us_cpi", NEUTRAL),
        ("kr_semiconductor_export_value", NEUTRAL),
    ]),
]

# Enough of the world to see whether a move is local or global, no more.
HEADLINE_INDICES = [
    "^KS11", "^KQ11", "^GSPC", "^IXIC", "^N225", "^HSI", "^STOXX50E", "^VIX",
]


def _tile(key: str, spec: dict, direction: str, points: list[dict]) -> dict | None:
    if not points:
        return None
    latest = points[-1]
    previous = points[-2] if len(points) > 1 else None
    change = None
    if previous is not None and previous.get("value") is not None:
        delta = round(latest["value"] - previous["value"], 4)
        # An unchanged policy rate is the normal case; printing "+0.000"
        # against every one of them buries the tiles that actually moved.
        change = delta if delta else None
    return {
        "key": key,
        "label": spec["label"],
        "unit": spec["unit"],
        "value": latest["value"],
        "date": latest["date"],
        "previous_date": previous["date"] if previous else None,
        "change": change,
        "direction": direction,
        "frequency": spec["frequency"],
        "proxy": spec["proxy"],
    }


def headline_tiles() -> list[dict]:
    """Group the core series a person checks first, in reading order."""
    catalog = indicators.catalog()
    wanted = [
        key for _, entries in HEADLINE_GROUPS for key, _ in entries
        if key in catalog
    ]
    tails = db.get_indicator_tails(wanted, limit=2)
    groups = []
    for title, entries in HEADLINE_GROUPS:
        tiles = []
        for key, direction in entries:
            spec = catalog.get(key)
            if not spec:
                continue
            tile = _tile(key, spec, direction, tails.get(key, []))
            if tile:
                tiles.append(tile)
        if tiles:
            groups.append({"title": title, "tiles": tiles})
    return groups


def headline_indices() -> list[dict]:
    """Latest index levels with a change computed from settled closes."""
    quotes = db.get_index_quotes()
    by_symbol = {item["symbol"]: item for item in indices.index_list()}
    rows = []
    for symbol in HEADLINE_INDICES:
        spec = by_symbol.get(symbol)
        quote = quotes.get(symbol)
        if not spec or not quote:
            continue
        price, previous = quote.get("price"), quote.get("prev_close")
        rows.append({
            "symbol": symbol,
            "name": spec["name"],
            "region": spec["region"],
            "value": price,
            "change_pct": (
                round((price - previous) / previous * 100, 2)
                if price is not None and previous not in (None, 0) else None
            ),
            "session_date": quote.get("session_date"),
        })
    return rows


def _spread(entry: dict | None, label: str, note: str) -> dict | None:
    if not entry:
        return None
    return {
        "label": label,
        "value": entry["value"],
        "unit": "%p",
        "date": entry["date"],
        "note": note,
    }


def risk_panel() -> dict:
    """Derived diagnostics that no single series shows on its own."""
    derived = market_metrics.derived_snapshot()
    macro = derived["macro"]
    items = []
    items.append(_spread(
        macro.get("kr_credit_spread_3y"), "한국 신용스프레드 3년",
        "회사채 3년 − 국고채 3년. 확대는 신용 경계 신호입니다.",
    ))
    items.append(_spread(
        macro.get("kr_curve_10y_3y"), "한국 국고채 10년−3년",
        "장단기 금리차. 축소·역전은 성장 기대 약화를 시사합니다.",
    ))
    items.append(_spread(
        macro.get("kr_cp_cd_spread"), "한국 CP−CD 91일",
        "단기 자금시장의 신용·유동성 긴장도. 확대는 조달 여건 악화입니다.",
    ))
    items.append(_spread(
        macro.get("kr_kofr_base_gap"), "한국 KOFR−기준금리",
        "무위험 조달금리와 정책금리의 괴리. 자금시장 수급 압력을 보여줍니다.",
    ))
    real_policy = macro.get("kr_real_policy_rate")
    if real_policy:
        items.append({
            "label": "한국 실질 정책금리",
            "value": real_policy["value_pct"],
            "unit": "%",
            "date": real_policy["base_rate"]["date"],
            "note": "기준금리 − CPI 전년비. 혼합 주기 최신값이며 동일 관측일 정렬이 아닙니다.",
        })
    liquidity = macro.get("us_net_liquidity")
    if liquidity:
        items.append({
            "label": "미국 순유동성",
            "value": round(liquidity["net_liquidity_million_usd"] / 1_000_000, 3),
            "unit": "조달러",
            "date": liquidity["as_of"],
            "note": "WALCL − TGA − ON RRP. 널리 쓰이는 proxy이며 시장 유동성 전체가 아닙니다.",
        })
    return {
        "items": [item for item in items if item],
        "cross_asset": derived["cross_asset"],
    }


def upcoming_events(days: int = 7) -> list[dict]:
    """High-impact releases inside the window a person can still act on."""
    start = kst_today()
    end = start + timedelta(days=days)
    events = db.get_events(start.isoformat(), end.isoformat())
    return [event for event in events if event.get("impact") == "high"]


def freshness() -> dict:
    """One line a person can trust the rest of the page against.

    This reads only the tail of each series, never the observations, so the
    badge costs nothing to render.  The full audit behind ``/api/coverage``
    additionally checks interior gaps against provider manifests.
    """
    today = kst_today()
    # The expected set comes from the code catalog, not from the mirrored
    # `series_catalog` table: a series that was never synced is missing, and
    # a system that has collected nothing must not report itself healthy.
    catalog = indicators.catalog()
    stored = db.get_indicator_overview()
    stale = missing = stalled = core_ready = core_total = 0
    for key, spec in catalog.items():
        latest = (stored.get(key) or {}).get("max_date")
        if spec["priority"] == "core":
            core_total += 1
        if not latest:
            missing += 1
            continue
        try:
            age = (today - date.fromisoformat(latest)).days
        except (TypeError, ValueError):
            missing += 1
            continue
        if age > spec["max_age_days"]:
            stale += 1
            continue
        # A widened allowance stops the repair loop chasing a provider that
        # has stopped publishing; it must not also hide that fact from the
        # person reading the numbers.
        if age > indicators.DEFAULT_MAX_AGE_DAYS.get(spec["frequency"], 100):
            stalled += 1
        if spec["priority"] == "core":
            core_ready += 1
    behind = sum(
        1 for row in db.index_freshness_summary().values()
        if row["session_date"] and row["latest"]
        and row["latest"] < row["session_date"]
    )
    return {
        "status": "ok" if not (missing or behind) else "incomplete",
        "core_ready": core_ready,
        "core_total": core_total,
        "stale": stale,
        "provider_stalled": stalled,
        "missing": missing,
        "indices_behind": behind,
        "last_collect": db.get_meta("last_collect"),
        # Instants are stored in UTC and presented in KST; the page should
        # not make the reader do that conversion.
        "last_collect_kst": (
            collected.strftime("%Y-%m-%d %H:%M")
            if (collected := to_kst(db.get_meta("last_collect"))) else None
        ),
    }


def situation() -> dict:
    """Everything the front page shows, resolved from cache in one pass."""
    regime = analysis.market_regime(
        db.get_indicator_points("us_vix"),
        db.get_indicator_points("us_ig_spread"),
        db.get_index_points("^GSPC"),
    )
    return {
        "regime": regime,
        "sentiment": sentiment.gauge(),
        "groups": headline_tiles(),
        "indices": headline_indices(),
        "risk": risk_panel(),
        "events": upcoming_events(),
        "freshness": freshness(),
        "as_of": kst_today().isoformat(),
        "cached": True,
    }
