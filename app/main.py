"""FastAPI dashboard and cache-only JSON API."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app import (
    analysis, correlation, coverage, dashboard, db, history_recovery,
    market_metrics, registry, sentiment, spillover,
)
from app.collectors import indices, indicators, krx, watchlist
from app.collectors.indicators import categories
from app.scheduler import Scheduler
from app.timeutil import kst_today


log = logging.getLogger("money")
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent
_scheduler: Scheduler | None = None


def _init_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = registry.build_scheduler()
    return _scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    task: asyncio.Task | None = None
    if os.environ.get("MONEY_DISABLE_SCHEDULER", "").lower() not in {"1", "true", "yes"}:
        # Do not block ASGI startup on slow external data providers.
        task = asyncio.create_task(_init_scheduler().loop())
    yield
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Money Market Intelligence", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _next_days(days: int = 30) -> tuple[date, date]:
    today = kst_today()
    return today, today + timedelta(days=days)


def _index_spec(symbol: str) -> dict:
    for item in indices.index_list():
        if item["symbol"] == symbol:
            return item
    raise HTTPException(status_code=404, detail=f"index not found: {symbol}")


def _recent_index_points(symbol: str, years: int) -> list[dict]:
    start = (kst_today() - timedelta(days=365 * years + 10)).isoformat()
    return db.get_index_points(symbol, start=start)


def _index_points_by_name(name: str, years: int = 2) -> list[dict]:
    for item in indices.index_list():
        if item["name"] == name:
            return _recent_index_points(item["symbol"], years)
    raise HTTPException(status_code=404, detail=f"index not found: {name}")


# ---------- Web ----------
@app.get("/", response_class=HTMLResponse)
def situation_page(request: Request):
    """Render the market state from cache, without a client round trip."""
    return templates.TemplateResponse(
        request, "index.html",
        {"situation": dashboard.situation(), "active": "home"},
    )


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request):
    start, end = _next_days()
    events = db.get_events(start.isoformat(), end.isoformat())
    next_event = None
    for event in events:
        # "Major" has to mean the impact the catalog assigned, otherwise the
        # highlight promotes whichever low-impact release happens to be first.
        if event.get("impact") != "high":
            continue
        event_date = date.fromisoformat(event["date"])
        if event_date >= start:
            next_event = {"event": event, "days": (event_date - start).days}
            break
    grouped: dict[str, list] = {}
    for event in events:
        grouped.setdefault(event["date"], []).append(event)
    return templates.TemplateResponse(
        request,
        "calendar.html",
        {
            "groups": grouped,
            "next_event": next_event,
            "today": start.isoformat(),
            "end": end.isoformat(),
            "active": "calendar",
        },
    )


@app.get("/api/situation")
def api_situation():
    """The same cache-only payload the front page renders."""
    return dashboard.situation()


@app.get("/rates", response_class=HTMLResponse)
def rates_page(request: Request):
    return templates.TemplateResponse(request, "rates.html", {"active": "rates"})


@app.get("/charts", response_class=HTMLResponse)
def charts(request: Request):
    return templates.TemplateResponse(
        request, "charts.html",
        {"keys": list(indicators.catalog()), "cats": categories(), "active": "charts"},
    )


@app.get("/stocks", response_class=HTMLResponse)
def stocks(request: Request):
    return templates.TemplateResponse(request, "stocks.html", {"active": "stocks"})


@app.get("/indices", response_class=HTMLResponse)
def indices_page(request: Request):
    return templates.TemplateResponse(request, "indices.html", {"active": "indices"})


@app.get("/markets", response_class=HTMLResponse)
def markets_page(request: Request):
    return templates.TemplateResponse(request, "markets.html", {"active": "markets"})


@app.get("/analysis", response_class=HTMLResponse)
def analysis_page(request: Request):
    return templates.TemplateResponse(request, "analysis.html", {"active": "analysis"})


@app.get("/correlation", response_class=HTMLResponse)
def correlation_page(request: Request):
    return templates.TemplateResponse(request, "correlation.html", {"active": "correlation"})


@app.get("/spillover", response_class=HTMLResponse)
def spillover_page(request: Request):
    return templates.TemplateResponse(request, "spillover.html", {"active": "spillover"})


@app.get("/data", response_class=HTMLResponse)
def data_page(request: Request):
    return templates.TemplateResponse(request, "data.html", {"active": "data"})


@app.get("/manage", response_class=HTMLResponse)
def manage_page(request: Request):
    return templates.TemplateResponse(request, "manage.html", {"active": "manage"})


# ---------- Health, events, scheduler ----------
@app.get("/api/health")
def api_health():
    with db.get_conn() as conn:
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
    states = db.get_collector_state()
    states = states if isinstance(states, list) else []
    issue_rows = [state for state in states if state["status"] in {"partial", "error"}]
    issues = [{
        key: state.get(key)
        for key in (
            "name", "status", "ok", "total", "last_attempt_at",
            "last_success_at", "error",
        )
    } for state in issue_rows]
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
        "collector_errors": [
            state["name"] for state in issues if state["status"] == "error"
        ],
        "collector_issues": issues,
        "last_collect": db.get_meta("last_collect"),
        "schema_version": db.get_meta("schema_version"),
        "coverage": indicators.coverage_summary(),
        "completeness": {
            key: value for key, value in completeness.items() if key != "attention"
        },
        "reconciliation": reconciliation,
        "historical_recovery": historical,
        "cached": True,
    }


@app.get("/api/coverage")
def api_coverage(
    detail: str = Query(default="summary", pattern="^(summary|indicators|indices)$"),
):
    """Report which observations are missing and whether they are recoverable."""
    if detail == "indicators":
        rows = coverage.indicator_coverage()
        return {"count": len(rows), "indicators": rows, "cached": True}
    if detail == "indices":
        rows = coverage.index_coverage()
        return {"count": len(rows), "indices": rows, "cached": True}
    return {**coverage.audit(), "deficits": coverage.deficits()}


@app.get("/api/events")
def api_events(
    start: date | None = None,
    end: date | None = None,
    country: str | None = Query(default=None, min_length=2, max_length=2),
    days: int | None = Query(default=None, ge=1, le=365),
):
    default_start, default_end = _next_days(days or 30)
    start = start or default_start
    end = end or default_end
    if end < start:
        raise HTTPException(status_code=422, detail="end must be on or after start")
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


@app.get("/api/scheduler")
def api_scheduler():
    scheduler = _init_scheduler()
    return {
        "reconciliation": scheduler.reconciliation_status(),
        "historical_recovery": history_recovery.status(),
        "collectors": [
            {
                "name": collector.name,
                "interval": collector.interval,
                "last_run": collector.last_run or None,
                "state": collector.state or {"status": "never"},
            }
            for collector in scheduler.collectors
        ]
    }


@app.get("/api/manage/overview")
def api_manage_overview():
    catalog = indicators.catalog()
    stored_ind = db.get_indicator_overview()
    with db.get_conn() as conn:
        idx_rows = conn.execute(
            "SELECT symbol, MAX(date) AS max_date, COUNT(*) AS n "
            "FROM index_prices GROUP BY symbol"
        ).fetchall()
        quote_rows = conn.execute("SELECT symbol, updated FROM quotes").fetchall()
    indicator_health = {}
    for key, spec in catalog.items():
        row = stored_ind.get(
            key, {"max_date": None, "n": 0, "retrieved_at": None}
        )
        cycle = indicators.cycle_of(key)
        max_age = spec["max_age_days"]
        try:
            age = (kst_today() - date.fromisoformat(row["max_date"])).days
            status = "fresh" if age <= max_age else "stale"
        except (TypeError, ValueError):
            age = None
            status = "missing" if not row["max_date"] else "invalid"
        indicator_health[key] = {
            "label": spec["label"],
            "category": spec["category"],
            "source": spec["source"],
            "source_series": spec["series"],
            "source_options": spec["source_options"],
            "source_url": spec["source_url"],
            "analysis_group": spec["analysis_group"],
            "priority": spec["priority"],
            "proxy": spec["proxy"],
            "max_date": row["max_date"],
            "n": row["n"],
            "retrieved_at": row.get("retrieved_at"),
            "frequency": cycle,
            "age_days": age,
            "max_age_days": max_age,
            "status": status,
        }
    market = db.market_overview("krx")
    return {
        "indicators": indicator_health,
        "coverage": indicators.coverage_summary(),
        "indices": {row["symbol"]: {"max_date": row["max_date"], "n": row["n"]} for row in idx_rows},
        "expected_indices": len(indices.index_list()),
        "quotes": {row["symbol"]: row["updated"] for row in quote_rows},
        "market_universe": {
            **market,
            "scope": os.environ.get("KRX_MARKET_SCOPE", "balanced"),
            "expected_datasets": len(krx.dataset_specs()),
        },
        "logs": db.get_collect_log(limit=50),
        "collector_state": db.get_collector_state(),
        "historical_recovery": history_recovery.status(),
        "intervals": {
            "quotes": registry.QUOTE_INTERVAL,
            "index_quotes": registry.INDEX_QUOTE_INTERVAL,
            "index_history": registry.INDEX_HISTORY_INTERVAL,
            "ind_daily": registry.IND_DAILY_INTERVAL,
            "ind_monthly": registry.IND_MONTHLY_INTERVAL,
            "ind_quarterly": registry.IND_QUARTERLY_INTERVAL,
            "krx_market": registry.KRX_MARKET_INTERVAL,
            "historical_recovery": history_recovery.HISTORY_INTERVAL,
        },
    }


# ---------- Provider-wide market universe (SQLite only) ----------
@app.get("/api/market/universe")
def api_market_universe(
    source: str | None = Query(default=None, max_length=20),
    dataset: str | None = Query(default=None, max_length=60),
    asset_type: str | None = Query(default=None, max_length=40),
    q: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=500, ge=1, le=5000),
):
    rows = db.get_market_instruments(
        source=source, dataset=dataset, asset_type=asset_type, query=q, limit=limit
    )
    return {
        "count": len(rows),
        "instruments": rows,
        "overview": db.market_overview(source),
        "cached": True,
    }


@app.get("/api/market/datasets")
def api_market_datasets():
    """List cached KRX daily tables and how much of each is stored."""
    stored = {row["dataset"]: row for row in db.get_market_dataset_summary("krx")}
    items = []
    for spec in krx.dataset_specs():
        row = stored.get(spec["dataset"], {})
        items.append({
            "dataset": spec["dataset"],
            "label": spec["label"],
            "asset_type": spec["asset_type"],
            "instruments": row.get("instruments", 0),
            "rows": row.get("rows", 0),
            "first_date": row.get("first_date"),
            "latest_date": row.get("latest_date"),
        })
    return {
        "count": len(items),
        "datasets": sorted(items, key=lambda item: -item["rows"]),
        "scope": os.environ.get("KRX_MARKET_SCOPE", "balanced"),
        "cached": True,
    }


@app.get("/api/market/daily")
def api_market_daily(
    source: str | None = Query(default=None, max_length=20),
    dataset: str | None = Query(default=None, max_length=60),
    symbol: str | None = Query(default=None, max_length=120),
    day: date | None = Query(default=None, alias="date"),
    limit: int = Query(default=500, ge=1, le=5000),
):
    rows = db.get_market_daily(
        source=source,
        dataset=dataset,
        symbol=symbol,
        day=day.isoformat() if day else None,
        limit=limit,
    )
    return {"count": len(rows), "rows": rows, "cached": True}


# ---------- Quotes and indices (SQLite only) ----------
@app.get("/api/quotes")
def api_quotes(category: str | None = Query(default=None, max_length=40)):
    rows = db.get_quotes()
    if category:
        rows = [row for row in rows if row.get("group_name") == category]
    for row in rows:
        price, previous = row.get("price"), row.get("prev_close")
        row["change_pct"] = (
            round((price - previous) / previous * 100, 2)
            if price is not None and previous not in (None, 0) else None
        )
    return {
        "count": len(rows),
        "quotes": rows,
        "as_of": max((row.get("updated") or "" for row in rows), default=None) or None,
        "source": "yahoo",
        "cached": True,
    }


@app.get("/api/quotes/categories")
def api_quotes_categories():
    return {
        "categories": [
            {"name": category, "desc": watchlist.category_description(category)}
            for category in watchlist.categories()
        ]
    }


@app.get("/api/quote/{symbol}")
def api_quote(symbol: str):
    allowed = {item["symbol"] for item in watchlist.watchlist()}
    if symbol not in allowed:
        raise HTTPException(status_code=404, detail=f"quote not found: {symbol}")
    quote = db.get_quote(symbol)
    if not quote:
        raise HTTPException(status_code=503, detail=f"quote is not collected yet: {symbol}")
    return quote


@app.get("/api/indices")
def api_indices():
    quote_map = db.get_index_quotes()
    items = []
    for spec in indices.index_list():
        points = db.get_index_points(spec["symbol"])
        quote = quote_map.get(spec["symbol"])
        value = quote["price"] if quote else (points[-1]["value"] if points else None)
        previous = quote.get("prev_close") if quote else None
        change = (
            round((value - previous) / previous * 100, 2)
            if value is not None and previous not in (None, 0) else None
        )
        items.append({
            **spec,
            "value": value,
            "change_pct": change,
            "points": len(points),
            "observation_date": points[-1]["date"] if points else None,
            "updated_at": quote.get("updated_at") if quote else None,
            "desc": indices.index_description(spec["symbol"]),
        })
    dates = [item["observation_date"] for item in items if item["observation_date"]]
    return {
        "count": len(items),
        "indices": items,
        "as_of": max(dates, default=None),
        "source": "yahoo",
        "cached": True,
    }


@app.get("/api/index/{symbol}")
def api_index(
    symbol: str,
    start: date | None = None,
    end: date | None = None,
):
    spec = _index_spec(symbol)
    if start and end and end < start:
        raise HTTPException(status_code=422, detail="end must be on or after start")
    points = db.get_index_points(
        symbol,
        start=start.isoformat() if start else None,
        end=end.isoformat() if end else None,
    )
    if not points:
        raise HTTPException(status_code=503, detail=f"index history is not collected yet: {symbol}")
    return {**spec, "points": points, "desc": indices.index_description(symbol)}


# ---------- Indicators ----------
@app.get("/api/categories")
def api_categories():
    return {"categories": categories()}


@app.get("/api/indicators")
def api_indicators(category: str | None = Query(default=None, max_length=20)):
    catalog = indicators.catalog()
    stored = db.get_indicator_overview()
    items = []
    for key, spec in catalog.items():
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
            "desc": indicators.indicator_description(key),
        })
    return {"count": len(items), "items": items, "cached": True}


@app.get("/api/indicator/categories")
def api_indicator_categories():
    return {
        "categories": [
            {"name": category, "desc": indicators.category_description(category)}
            for category in categories()
        ]
    }


@app.get("/api/indicator/{key}")
def api_indicator(
    key: str,
    start: date | None = None,
    end: date | None = None,
    limit: int | None = Query(default=None, ge=1, le=1000),
):
    catalog = indicators.catalog()
    if key not in catalog:
        raise HTTPException(status_code=404, detail=f"unknown indicator: {key}")
    if start and end and end < start:
        raise HTTPException(status_code=422, detail="end must be on or after start")
    spec = catalog[key]
    stored = db.get_indicator_overview().get(key) or {}
    points = db.get_indicator_points(
        key,
        start=start.isoformat() if start else None,
        end=end.isoformat() if end else None,
    )
    if limit is not None:
        points = points[-limit:]
    return {
        "key": key,
        "label": spec["label"],
        "unit": spec["unit"],
        "source": spec["source"],
        "source_series": spec["series"],
        "source_options": spec["source_options"],
        "frequency": indicators.cycle_of(key),
        "max_age_days": spec["max_age_days"],
        "source_url": spec["source_url"],
        "analysis_group": spec["analysis_group"],
        "priority": spec["priority"],
        "proxy": spec["proxy"],
        "future": spec.get("future", []),
        "points": points,
        "desc": indicators.indicator_description(key),
        "latest_date": points[-1]["date"] if points else None,
        "retrieved_at": stored.get("retrieved_at"),
        "cached": True,
    }


# ---------- Correlation and connectedness ----------
@app.get("/api/correlation/indices")
def api_corr_indices():
    return {"indices": indices.index_list()}


@app.get("/api/correlation/matrix")
def api_corr_matrix(region: str | None = Query(default=None, max_length=20)):
    series = {}
    for spec in indices.index_list():
        if region and spec["region"] != region:
            continue
        points = _recent_index_points(spec["symbol"], 2)
        if len(points) > 120:
            series[spec["name"]] = points
    result = correlation.correlation_matrix(series)
    if result.get("error"):
        raise HTTPException(status_code=503, detail=result["error"])
    return result


@app.get("/api/correlation/rolling")
def api_corr_rolling(
    a: str = Query(min_length=1, max_length=80),
    b: str = Query(min_length=1, max_length=80),
    window: int = Query(default=60, ge=20, le=252),
):
    result = correlation.rolling_correlation(
        _index_points_by_name(a), _index_points_by_name(b), window
    )
    if result.get("error"):
        raise HTTPException(status_code=422, detail=result["error"])
    return {
        **result,
        "as_of": result["dates"][-1] if result.get("dates") else None,
        "source": "yahoo",
        "cached": True,
    }


@app.get("/api/correlation/leadlag")
def api_corr_leadlag(
    a: str = Query(min_length=1, max_length=80),
    b: str = Query(min_length=1, max_length=80),
    max_lag: int = Query(default=10, ge=0, le=20),
):
    result = correlation.lead_lag(
        _index_points_by_name(a), _index_points_by_name(b), max_lag
    )
    if result.get("error"):
        raise HTTPException(status_code=422, detail=result["error"])
    return {
        **result,
        "source": "yahoo",
        "cached": True,
        "warning": "시차 상관은 예측력이나 인과관계의 증거가 아닙니다.",
    }


@app.get("/api/spillover")
def api_spillover(
    region: str | None = Query(default=None, max_length=20),
    maxlags: int = Query(default=2, ge=1, le=10),
    horizon: int = Query(default=10, ge=1, le=50),
):
    series = {}
    for spec in indices.index_list():
        if region and spec["region"] != region:
            continue
        points = _recent_index_points(spec["symbol"], 2)
        if len(points) > 200:
            series[spec["name"]] = points
    result = spillover.spillover_network(series, maxlags=maxlags, horizon=horizon)
    if result.get("error"):
        raise HTTPException(status_code=422, detail=result["error"])
    return {**result, "source": "yahoo", "cached": True}


# ---------- Analysis ----------
@app.get("/api/analysis/yield_curve")
def api_yield_curve(country: str = Query(default="us", pattern="^(us|kr)$")):
    if country == "kr":
        # 3y-10y is the conventional Korean term spread. The old 3y-5y pair
        # only existed because the ten-year was not being collected.
        short = db.get_indicator_points("kr_treasury_3y")
        long = db.get_indicator_points("kr_treasury_10y")
        label = "한국"
    else:
        short = db.get_indicator_points("us_2y")
        long = db.get_indicator_points("us_10y")
        label = "미국"
    result = analysis.term_spread(short, long)
    if not result:
        raise HTTPException(status_code=503, detail="not enough aligned yield data")
    result.update({
        "country": label,
        "source": "ecos" if country == "kr" else "fred",
        "cached": True,
        "warning": "금리차 역전만으로 경기침체를 확정하거나 시점을 예측할 수 없습니다.",
    })
    return result


@app.get("/api/analysis/curve")
def api_curve(country: str = Query(default="kr", pattern="^(us|kr)$")):
    """Return an aligned sovereign yield curve with a one-month comparison."""
    keys = [key for _, key, _ in analysis.CURVE_TENORS[country]]
    result = analysis.yield_curve(
        {key: db.get_indicator_points(key) for key in keys}, country
    )
    if result.get("error"):
        raise HTTPException(status_code=503, detail=result["error"])
    return {
        **result,
        "source": "ecos" if country == "kr" else "fred",
        "cached": True,
        "warning": "곡선 역전만으로 경기침체를 확정하거나 시점을 예측할 수 없습니다.",
    }


@app.get("/api/analysis/trend")
def api_trend(symbol: str, years: int = Query(default=2, ge=1, le=20)):
    spec = _index_spec(symbol)
    points = _recent_index_points(symbol, years)
    result = analysis.full_analysis(points)
    if result.get("error"):
        raise HTTPException(status_code=503, detail=result["error"])
    return {
        **result,
        "name": spec["name"],
        "symbol": symbol,
        "as_of": points[-1]["date"] if points else None,
        "source": "yahoo",
        "years_requested": years,
        "cached": True,
    }


@app.get("/api/analysis/volatility")
def api_volatility(symbol: str, years: int = Query(default=2, ge=1, le=20)):
    spec = _index_spec(symbol)
    points = _recent_index_points(symbol, years)
    return {
        "symbol": symbol,
        "name": spec["name"],
        "volatility": analysis.realized_volatility(points),
        "max_drawdown": analysis.max_drawdown(points),
        "as_of": points[-1]["date"] if points else None,
        "source": "yahoo",
        "years_requested": years,
        "cached": True,
    }


@app.get("/api/analysis/technical")
def api_technical(symbol: str, years: int = Query(default=2, ge=1, le=20)):
    spec = _index_spec(symbol)
    points = _recent_index_points(symbol, years)
    return {
        "symbol": symbol,
        "name": spec["name"],
        "rsi": analysis.rsi(points),
        "macd": analysis.macd(points),
        "bollinger": analysis.bollinger(points),
        "trend": analysis.trend_analysis(points),
        "as_of": points[-1]["date"] if points else None,
        "source": "yahoo",
        "years_requested": years,
        "cached": True,
        "warning": "기술 지표는 후행적 기술 통계이며 매수·매도 신호가 아닙니다.",
    }


@app.get("/api/analysis/risk")
def api_risk(symbol: str, years: int = Query(default=2, ge=1, le=20)):
    spec = _index_spec(symbol)
    points = _recent_index_points(symbol, years)
    return {
        "symbol": symbol,
        "name": spec["name"],
        "sharpe": analysis.sharpe_ratio(points),
        "var": analysis.value_at_risk(points),
        "max_drawdown": analysis.max_drawdown(points),
        "as_of": points[-1]["date"] if points else None,
        "source": "yahoo",
        "years_requested": years,
        "cached": True,
        "warning": "VaR는 과거 수익률 분위수이며 최대 예상 손실이 아닙니다.",
    }


@app.get("/api/analysis/regime")
def api_regime():
    result = analysis.market_regime(
        db.get_indicator_points("us_vix"),
        db.get_indicator_points("us_ig_spread"),
        _recent_index_points("^GSPC", 2),
    )
    return {
        **result,
        "cached": True,
        "warning": "임계값 기반 참고 분류이며 현재 시장의 객관적 정답이 아닙니다.",
    }


@app.get("/api/analysis/sentiment")
def api_sentiment():
    """Korean market sentiment gauge composed from this project's own inputs."""
    return sentiment.gauge()


@app.get("/api/analysis/derived")
def api_derived_metrics():
    return market_metrics.derived_snapshot()


@app.get("/api/analysis/krx-breadth")
def api_krx_breadth():
    return market_metrics.krx_breadth_snapshot()
