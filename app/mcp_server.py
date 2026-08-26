"""Cache-only MCP server for Money Market Intelligence.

The tools intentionally never trigger provider collection.  Each analytical
response exposes its observation date, source, and method where applicable so
an agent can distinguish cached evidence from a live quote or a causal claim.
"""
from __future__ import annotations

from datetime import timedelta

from mcp.server import MCPServer

from app import (
    analysis, correlation, coverage, dashboard, db, history_recovery,
    market_metrics, sentiment as sentiment_gauge,
    spillover as spillover_analysis,
)
from app.collectors import indices, indicators
from app.timeutil import kst_today


mcp = MCPServer(
    "Money Market Intelligence",
    instructions=(
        "무료 데이터 소스에서 SQLite로 수집한 금융·경제 데이터를 읽습니다. "
        "결과의 관측일과 출처를 확인하고 투자 조언으로 단정하지 마세요."
    ),
)


def _index_spec(symbol: str) -> dict | None:
    return next((item for item in indices.index_list() if item["symbol"] == symbol), None)


def _index_spec_by_name(name: str) -> dict | None:
    return next((item for item in indices.index_list() if item["name"] == name), None)


def _recent_index_points(symbol: str, years: int = 2) -> list[dict]:
    start = (kst_today() - timedelta(days=365 * years + 10)).isoformat()
    return db.get_index_points(symbol, start=start)


def _analysis_metadata(points: list[dict], years: int) -> dict:
    return {
        "as_of": points[-1]["date"] if points else None,
        "source": "yahoo",
        "years_requested": years,
        "cached": True,
    }


@mcp.tool()
def market_health() -> dict:
    """Check cache integrity and collection state before trusting missing/stale data."""
    db.init_db()
    with db.get_conn() as conn:
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
    states = db.get_collector_state()
    issue_rows = [row for row in states if row["status"] in {"partial", "error"}]
    issues = [{
        key: row.get(key)
        for key in (
            "name", "status", "ok", "total", "last_attempt_at",
            "last_success_at", "error",
        )
    } for row in issue_rows]
    reconciliation = db.get_reconciliation_state()
    historical = history_recovery.status()
    completeness = coverage.audit()
    return {
        "status": (
            "ok" if integrity == "ok" and not issues
            and reconciliation["status"] != "pending"
            and historical["status"] == "ok"
            and completeness["status"] == "ok" else "degraded"
        ),
        "database_integrity": integrity,
        "schema_version": db.get_meta("schema_version"),
        "last_collect": db.get_meta("last_collect"),
        "coverage": indicators.coverage_summary(),
        "completeness": {
            key: value for key, value in completeness.items() if key != "attention"
        },
        "collector_errors": [row["name"] for row in issues if row["status"] == "error"],
        "collector_issues": issues,
        "reconciliation": reconciliation,
        "historical_recovery": historical,
        "cached": True,
    }


@mcp.tool()
def market_situation() -> dict:
    """Summarize the whole market state in one call before drilling into series.

    Returns the regime verdict, core policy/funding/risk levels with their
    observation dates, derived spreads and liquidity, this week's high-impact
    releases, and a freshness line.  Prefer this over a dozen single-series
    calls when the question is "what is going on right now".
    """
    report = dashboard.situation()
    return {
        **report,
        "guidance": (
            "각 값에는 관측일이 붙어 있습니다. 서로 다른 주기의 계열을 같은 시점처럼 "
            "비교하지 말고, regime은 임계값 기반 참고 분류로만 인용하세요."
        ),
    }


@mcp.tool()
def market_coverage(key: str | None = None) -> dict:
    """Report missing observations and whether the collector can still recover them.

    Call this before treating an absence as a market fact.  A gap marked
    `confirmed` is one the provider has and we lack; `candidate` is implied by
    the series' own cadence and may simply never have been published;
    `unverifiable` means the provider's session list has not been captured yet.
    """
    if key:
        rows = [row for row in coverage.indicator_coverage() if row["key"] == key]
        if not rows:
            symbols = [
                row for row in coverage.index_coverage() if row["symbol"] == key
            ]
            if symbols:
                return {**symbols[0], "cached": True}
            return {
                "error": f"unknown series: {key}",
                "hint": "use market_indicator_list or market_indices to discover ids",
            }
        return {**rows[0], "cached": True}
    report = coverage.audit()
    return {
        **report,
        "guidance": (
            "핵심 계열이 준비되지 않았거나 confirmed 결측이 있으면 결론의 확신도를 "
            "낮추고 그 사실을 답변에 밝히세요. 결측을 보간하거나 추정치로 대체하지 "
            "마세요."
        ),
    }


@mcp.tool()
def market_events(days: int = 30, country: str | None = None) -> dict:
    """List cached official economic events in Asia/Seoul time (1-365 days)."""
    if not 1 <= days <= 365:
        return {"error": "days must be between 1 and 365"}
    start = kst_today()
    end = start + timedelta(days=days)
    events = db.get_events(start.isoformat(), end.isoformat())
    if country:
        events = [event for event in events if event["country"] == country.upper()]
    return {
        "timezone": "Asia/Seoul",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": len(events),
        "events": events,
        "cached": True,
    }


@mcp.tool()
def market_quotes(category: str | None = None) -> dict:
    """Return cached watchlist quotes; updated is retrieval time, not exchange time."""
    rows = db.get_quotes()
    if category:
        rows = [row for row in rows if row["group_name"] == category]
    for row in rows:
        price, previous = row.get("price"), row.get("prev_close")
        row["change_pct"] = (
            round((price - previous) / previous * 100, 2)
            if price is not None and previous not in (None, 0)
            else None
        )
    return {
        "count": len(rows),
        "quotes": rows,
        "as_of": max((row.get("updated") or "" for row in rows), default=None) or None,
        "source": "yahoo",
        "cached": True,
    }


@mcp.tool()
def market_indices() -> dict:
    """Return cached allowlisted global indices with observation and retrieval dates."""
    quote_map = db.get_index_quotes()
    result = []
    for spec in indices.index_list():
        points = db.get_index_points(spec["symbol"])
        quote = quote_map.get(spec["symbol"])
        value = quote["price"] if quote else (points[-1]["value"] if points else None)
        previous = quote.get("prev_close") if quote else None
        change = (
            round((value - previous) / previous * 100, 2)
            if value is not None and previous not in (None, 0)
            else None
        )
        result.append({
            **spec,
            "value": value,
            "change_pct": change,
            "observations": len(points),
            "observation_date": points[-1]["date"] if points else None,
            "updated_at": quote.get("updated_at") if quote else None,
        })
    dates = [item["observation_date"] for item in result if item["observation_date"]]
    return {
        "count": len(result),
        "indices": result,
        "as_of": max(dates, default=None),
        "source": "yahoo",
        "cached": True,
    }


@mcp.tool()
def market_indicator_list(category: str | None = None) -> dict:
    """Discover indicator keys and metadata before requesting a specific series."""
    known_categories = indicators.categories()
    if category and category not in known_categories:
        return {"error": f"unknown category: {category}", "available": known_categories}
    stored = db.get_indicator_overview()
    items = []
    for key, spec in indicators.catalog().items():
        if category and spec["category"] != category:
            continue
        row = stored.get(key) or {}
        items.append({
            "key": key,
            "label": spec["label"],
            "unit": spec["unit"],
            "category": spec["category"],
            "source": spec["source"],
            "source_series": spec["series"],
            "source_options": spec["source_options"],
            "source_url": spec["source_url"],
            "frequency": indicators.cycle_of(key),
            "max_age_days": spec["max_age_days"],
            "latest_date": row.get("max_date"),
            "retrieved_at": row.get("retrieved_at"),
            "analysis_group": spec["analysis_group"],
            "priority": spec["priority"],
            "proxy": spec["proxy"],
            "description": indicators.indicator_description(key),
        })
    return {"count": len(items), "items": items, "cached": True}


@mcp.tool()
def market_indicator(key: str, limit: int = 24) -> dict:
    """Return up to 1000 cached observations and metadata for one indicator key."""
    catalog = indicators.catalog()
    if key not in catalog:
        return {"error": f"unknown indicator: {key}", "available": sorted(catalog)}
    if not 1 <= limit <= 1000:
        return {"error": "limit must be between 1 and 1000"}
    spec = catalog[key]
    stored = db.get_indicator_overview().get(key) or {}
    points = db.get_indicator_points(key)[-limit:]
    return {
        "key": key,
        "label": spec["label"],
        "unit": spec["unit"],
        "source": spec["source"],
        "source_series": spec["series"],
        "source_options": spec["source_options"],
        "source_url": spec["source_url"],
        "frequency": indicators.cycle_of(key),
        "max_age_days": spec["max_age_days"],
        "analysis_group": spec["analysis_group"],
        "priority": spec["priority"],
        "proxy": spec["proxy"],
        "description": indicators.indicator_description(key),
        "latest_date": points[-1]["date"] if points else None,
        "retrieved_at": stored.get("retrieved_at"),
        "points": points,
        "cached": True,
    }


@mcp.tool()
def market_correlation(
    a: str,
    b: str,
    window: int = 60,
    max_lag: int = 10,
) -> dict:
    """Compare two index names using cached return correlation; never infer causality."""
    left_spec = _index_spec_by_name(a)
    right_spec = _index_spec_by_name(b)
    if not left_spec or not right_spec:
        return {
            "error": "a and b must be exact index names",
            "available": [item["name"] for item in indices.index_list()],
        }
    if not 20 <= window <= 252:
        return {"error": "window must be between 20 and 252"}
    if not 0 <= max_lag <= 20:
        return {"error": "max_lag must be between 0 and 20"}
    left = _recent_index_points(left_spec["symbol"])
    right = _recent_index_points(right_spec["symbol"])
    rolling = correlation.rolling_correlation(left, right, window)
    lead_lag = correlation.lead_lag(left, right, max_lag)
    dates = rolling.get("dates", [])
    return {
        "a": left_spec,
        "b": right_spec,
        "rolling": rolling,
        "lead_lag": lead_lag,
        "as_of": dates[-1] if dates else None,
        "source": "yahoo",
        "cached": True,
        "warning": (
            "상관 및 시차 상관은 표본 내 기술 통계이며 예측력이나 인과관계의 "
            "증거가 아닙니다. 시차에는 거래 시간대 차이가 남아 있습니다."
        ),
    }


@mcp.tool()
def market_spillover(
    region: str | None = None,
    maxlags: int = 2,
    horizon: int = 10,
) -> dict:
    """Estimate cached generalized-FEVD connectedness; direction is not causality."""
    known_regions = sorted({item["region"] for item in indices.index_list()})
    if region and region not in known_regions:
        return {"error": f"unknown region: {region}", "available_regions": known_regions}
    if not 1 <= maxlags <= 10:
        return {"error": "maxlags must be between 1 and 10"}
    if not 1 <= horizon <= 50:
        return {"error": "horizon must be between 1 and 50"}
    series = {}
    for spec in indices.index_list():
        if region and spec["region"] != region:
            continue
        points = _recent_index_points(spec["symbol"])
        if len(points) > 200:
            series[spec["name"]] = points
    result = spillover_analysis.spillover_network(
        series, maxlags=maxlags, horizon=horizon
    )
    return {
        **result,
        "source": "yahoo",
        "cached": True,
        "scope": {"region": region, "series": list(series)},
    }


@mcp.tool()
def market_yield_curve(country: str = "us") -> dict:
    """Return a cached aligned US/Korean term spread; not a standalone forecast."""
    if country.lower() == "kr":
        short_key, long_key, label = "kr_treasury_3y", "kr_treasury_10y", "한국"
    elif country.lower() == "us":
        short_key, long_key, label = "us_2y", "us_10y", "미국"
    else:
        return {"error": "country must be 'us' or 'kr'"}
    result = analysis.term_spread(
        db.get_indicator_points(short_key), db.get_indicator_points(long_key)
    )
    if not result:
        return {"error": "not enough aligned yield data"}
    return {
        **result,
        "country": label,
        "short_series": short_key,
        "long_series": long_key,
        "source": indicators.catalog()[short_key]["source"],
        "cached": True,
        "warning": "금리차 역전만으로 경기침체를 확정하거나 시점을 예측할 수 없습니다.",
    }


@mcp.tool()
def market_regime() -> dict:
    """Classify cached conditions with a rule-based heuristic, not investment advice."""
    result = analysis.market_regime(
        db.get_indicator_points("us_vix"),
        db.get_indicator_points("us_ig_spread"),
        db.get_index_points("^GSPC"),
    )
    return {
        **result,
        "cached": True,
        "warning": "임계값 기반 참고 분류이며 현재 시장의 객관적 정답이 아닙니다.",
    }


@mcp.tool()
def market_sentiment() -> dict:
    """Score Korean risk appetite 0-100 from this project's own collected inputs.

    Unlike a vendor sentiment index, every component, threshold, and omission
    is stated: `components` lists what was measured and how, `pending` lists
    what could not be and why.  Quote the component spread, not just the
    headline — a composite hides the disagreement that matters.
    """
    report = sentiment_gauge.gauge()
    return {
        **report,
        "guidance": (
            "구성요소가 서로 어긋날 때가 가장 유용한 정보입니다. 예를 들어 "
            "시장 폭은 탐욕인데 신용 수요가 공포라면 주가 상승이 신용시장의 "
            "확인을 받지 못한 상태입니다. 합성 점수만 인용하지 마세요."
        ),
    }


@mcp.tool()
def market_derived_metrics() -> dict:
    """Return cached macro transformations and cross-asset relative strength."""
    return market_metrics.derived_snapshot()


@mcp.tool()
def market_breadth() -> dict:
    """Return cached KOSPI/KOSDAQ breadth derived from official KRX stock rows."""
    return market_metrics.krx_breadth_snapshot()


@mcp.tool()
def market_index_analysis(symbol: str, years: int = 2) -> dict:
    """Describe cached index trend, volatility, and drawdown; not a trade signal."""
    spec = _index_spec(symbol)
    if not spec:
        return {
            "error": f"unknown index symbol: {symbol}",
            "available": [item["symbol"] for item in indices.index_list()],
        }
    if not 1 <= years <= 20:
        return {"error": "years must be between 1 and 20"}
    points = _recent_index_points(symbol, years)
    result = analysis.full_analysis(points)
    return {
        **result,
        "name": spec["name"],
        "symbol": symbol,
        **_analysis_metadata(points, years),
    }


@mcp.tool()
def market_technical(symbol: str, years: int = 2) -> dict:
    """Return descriptive RSI, MACD, Bollinger, and trend values for an allowed index."""
    spec = _index_spec(symbol)
    if not spec:
        return {
            "error": f"unknown index symbol: {symbol}",
            "available": [item["symbol"] for item in indices.index_list()],
        }
    if not 1 <= years <= 20:
        return {"error": "years must be between 1 and 20"}
    points = _recent_index_points(symbol, years)
    return {
        "symbol": symbol,
        "name": spec["name"],
        "rsi": analysis.rsi(points),
        "macd": analysis.macd(points),
        "bollinger": analysis.bollinger(points),
        "trend": analysis.trend_analysis(points),
        **_analysis_metadata(points, years),
        "warning": "기술 지표는 후행적 기술 통계이며 매수·매도 신호가 아닙니다.",
    }


@mcp.tool()
def market_risk(symbol: str, years: int = 2) -> dict:
    """Return cached historical risk metrics for an allowed index."""
    spec = _index_spec(symbol)
    if not spec:
        return {
            "error": f"unknown index symbol: {symbol}",
            "available": [item["symbol"] for item in indices.index_list()],
        }
    if not 1 <= years <= 20:
        return {"error": "years must be between 1 and 20"}
    points = _recent_index_points(symbol, years)
    return {
        "symbol": symbol,
        "name": spec["name"],
        "sharpe": analysis.sharpe_ratio(points),
        "var": analysis.value_at_risk(points),
        "max_drawdown": analysis.max_drawdown(points),
        **_analysis_metadata(points, years),
        "warning": (
            "VaR는 과거 수익률 분위수이며 최대 예상 손실이 아닙니다. "
            "샤프 비율과 낙폭도 표본 기간에 민감합니다."
        ),
    }


@mcp.tool()
def market_universe(
    query: str | None = None,
    source: str | None = "krx",
    dataset: str | None = None,
    asset_type: str | None = None,
    limit: int = 100,
) -> dict:
    """Search the provider-discovered cached instrument universe, including KRX."""
    if not 1 <= limit <= 5000:
        return {"error": "limit must be between 1 and 5000"}
    rows = db.get_market_instruments(
        source=source,
        dataset=dataset,
        asset_type=asset_type,
        query=query,
        limit=limit,
    )
    return {
        "count": len(rows),
        "instruments": rows,
        "overview": db.market_overview(source),
        "filters": {
            "query": query,
            "source": source,
            "dataset": dataset,
            "asset_type": asset_type,
            "limit": limit,
        },
        "cached": True,
    }


def main() -> None:
    db.init_db()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
