"""Cache-only market metrics composed from collected provider series.

Raw observations stay in their provider-backed tables.  This module aligns
dates, normalizes units, and derives reusable diagnostics for REST and MCP so
the same finance rules are not duplicated across interfaces.
"""
from __future__ import annotations

from collections import defaultdict

from app import db


def _latest(points: list[dict]) -> dict | None:
    return points[-1] if points else None


def _latest_for(key: str) -> dict | None:
    return _latest(db.get_indicator_points(key))


def _pct_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return round((current / previous - 1) * 100, 3)


def _growth_summary(key: str, annual_periods: int = 12) -> dict | None:
    points = db.get_indicator_points(key)
    if not points:
        return None
    current = points[-1]
    result = {
        "date": current["date"],
        "level": current["value"],
        "mom_pct": None,
        "yoy_pct": None,
    }
    if len(points) >= 2:
        result["mom_pct"] = _pct_change(current["value"], points[-2]["value"])
    if len(points) > annual_periods:
        result["yoy_pct"] = _pct_change(
            current["value"], points[-annual_periods - 1]["value"]
        )
    return result


def _aligned_difference(left_key: str, right_key: str) -> dict | None:
    left = {point["date"]: point["value"] for point in db.get_indicator_points(left_key)}
    right = {point["date"]: point["value"] for point in db.get_indicator_points(right_key)}
    common = sorted(left.keys() & right.keys())
    if not common:
        return None
    day = common[-1]
    return {
        "date": day,
        "value": round(left[day] - right[day], 3),
        "left": {"key": left_key, "value": left[day]},
        "right": {"key": right_key, "value": right[day]},
    }


def _aligned_levels(keys: list[str]) -> tuple[str, dict[str, dict]] | None:
    series = {key: db.get_indicator_points(key) for key in keys}
    if any(not points for points in series.values()):
        return None
    target = min(points[-1]["date"] for points in series.values())
    aligned: dict[str, dict] = {}
    for key, points in series.items():
        candidates = [point for point in points if point["date"] <= target]
        if not candidates:
            return None
        aligned[key] = candidates[-1]
    return target, aligned


def _relative_strength(
    left: list[dict],
    right: list[dict],
    *,
    left_key: str,
    right_key: str,
) -> dict | None:
    left_by_date = {point["date"]: point["value"] for point in left}
    right_by_date = {point["date"]: point["value"] for point in right}
    dates = sorted(left_by_date.keys() & right_by_date.keys())
    ratios = [
        (day, left_by_date[day] / right_by_date[day])
        for day in dates
        if right_by_date[day] != 0
    ]
    if len(ratios) < 2:
        return None
    current_day, current = ratios[-1]
    changes = {}
    for window in (20, 60):
        changes[f"relative_{window}d_pct"] = (
            _pct_change(current, ratios[-window - 1][1])
            if len(ratios) > window else None
        )
    return {
        "left": left_key,
        "right": right_key,
        "as_of": current_day,
        "observations": len(ratios),
        **changes,
    }


def derived_snapshot() -> dict:
    """Return mixed-frequency macro and cross-asset diagnostics."""
    cpi = _growth_summary("us_cpi")
    core_cpi = _growth_summary("us_core_cpi")
    pce = _growth_summary("us_pce")
    core_pce = _growth_summary("us_core_pce")

    base_rate = _latest_for("kr_base_rate")
    kr_cpi = _growth_summary("kr_cpi")
    real_policy = None
    if base_rate and kr_cpi and kr_cpi["yoy_pct"] is not None:
        real_policy = {
            "value_pct": round(base_rate["value"] - kr_cpi["yoy_pct"], 3),
            "base_rate": base_rate,
            "cpi_yoy_pct": kr_cpi["yoy_pct"],
            "cpi_date": kr_cpi["date"],
            "warning": "혼합 주기 최신값 차이이며 동일 관측일 정렬값이 아닙니다.",
        }

    per = _latest_for("kr_kospi_per")
    treasury = _latest_for("kr_treasury_3y")
    equity_risk_premium = None
    if per and per["value"] > 0 and treasury:
        earnings_yield = 100 / per["value"]
        equity_risk_premium = {
            "value_pct": round(earnings_yield - treasury["value"], 3),
            "earnings_yield_pct": round(earnings_yield, 3),
            "per": per,
            "treasury_3y": treasury,
            "warning": "월간 PER과 일별 국고채 최신값을 결합한 단순 proxy입니다.",
        }

    nfp = db.get_indicator_points("us_nfp")
    nfp_change = None
    if len(nfp) >= 2:
        nfp_change = {
            "date": nfp[-1]["date"],
            "change_thousand": round(nfp[-1]["value"] - nfp[-2]["value"], 1),
            "level_thousand": nfp[-1]["value"],
        }

    liquidity = None
    aligned = _aligned_levels(["us_fed_assets", "us_tga", "us_on_rrp"])
    if aligned:
        day, values = aligned
        fed_assets = values["us_fed_assets"]["value"]
        tga = values["us_tga"]["value"]
        on_rrp_million = values["us_on_rrp"]["value"] * 1000
        liquidity = {
            "as_of": day,
            "net_liquidity_million_usd": round(fed_assets - tga - on_rrp_million, 3),
            "components": values,
            "formula": "WALCL - WTREGEN - (RRPONTSYD * 1000)",
            "warning": "널리 쓰이는 단순 proxy이며 시장 유동성 전체를 뜻하지 않습니다.",
        }
        reserve = _latest_for("us_reserve_balances")
        if reserve:
            liquidity["reserve_balances"] = reserve

    sp500 = db.get_index_points("^GSPC")
    relative_pairs = {
        "equal_weight_vs_sp500": (
            db.get_indicator_points("us_equal_weight_proxy"), sp500,
            "us_equal_weight_proxy", "^GSPC",
        ),
        "semiconductor_vs_sp500": (
            db.get_indicator_points("us_semiconductor_proxy"), sp500,
            "us_semiconductor_proxy", "^GSPC",
        ),
        "regional_banks_vs_sp500": (
            db.get_indicator_points("us_regional_bank_proxy"), sp500,
            "us_regional_bank_proxy", "^GSPC",
        ),
        "high_yield_vs_investment_grade": (
            db.get_indicator_points("us_high_yield_proxy"),
            db.get_indicator_points("us_lqd_proxy"),
            "us_high_yield_proxy", "us_lqd_proxy",
        ),
        "discretionary_vs_staples": (
            db.get_indicator_points("us_discretionary_proxy"),
            db.get_indicator_points("us_staples_proxy"),
            "us_discretionary_proxy", "us_staples_proxy",
        ),
    }
    cross_asset = {
        name: _relative_strength(left, right, left_key=left_key, right_key=right_key)
        for name, (left, right, left_key, right_key) in relative_pairs.items()
    }

    required = {
        "us_tga", "us_on_rrp", "us_reserve_balances", "us_lqd_proxy",
        "us_regional_bank_proxy", "us_discretionary_proxy", "us_staples_proxy",
    }
    overview = db.get_indicator_overview()
    return {
        "macro": {
            "kr_credit_spread_3y": _aligned_difference(
                "kr_corp_bond_3y", "kr_treasury_3y"
            ),
            "kr_curve_10y_3y": _aligned_difference(
                "kr_treasury_10y", "kr_treasury_3y"
            ),
            "kr_curve_3y_1y": _aligned_difference(
                "kr_treasury_3y", "kr_treasury_1y"
            ),
            "kr_credit_spread_bbb": _aligned_difference(
                "kr_corp_bond_bbb", "kr_treasury_3y"
            ),
            "kr_kofr_base_gap": _aligned_difference(
                "kr_kofr", "kr_base_rate"
            ),
            "kr_cp_cd_spread": _aligned_difference(
                "kr_cp_91d", "kr_cd_91d"
            ),
            "kr_real_policy_rate": real_policy,
            "kr_kospi_equity_risk_premium": equity_risk_premium,
            "us_net_liquidity": liquidity,
            "us_nfp_monthly_change": nfp_change,
            "us_cpi": cpi,
            "us_core_cpi": core_cpi,
            "us_pce": pce,
            "us_core_pce": core_pce,
        },
        "cross_asset": cross_asset,
        "missing_inputs": sorted(required - set(overview)),
        "cached": True,
        "method": "latest aligned observations; mixed-frequency exceptions are labeled",
    }


def _concentration(rows: list[dict], field: str) -> float | None:
    values = sorted(
        (float(row[field]) for row in rows if row.get(field) is not None and row[field] > 0),
        reverse=True,
    )
    total = sum(values)
    return round(sum(values[:10]) / total * 100, 2) if total else None


def _breadth_market(dataset: str, label: str) -> dict:
    latest = db.get_latest_market_daily("krx", dataset)
    rows = latest["rows"]
    if not rows:
        return {
            "market": label,
            "dataset": dataset,
            "status": "unavailable",
            "as_of": None,
            "reason": "KRX 승인 데이터가 캐시에 없습니다.",
        }

    changed = [row for row in rows if row.get("change_pct") is not None]
    advances = [row for row in changed if row["change_pct"] > 0]
    declines = [row for row in changed if row["change_pct"] < 0]
    unchanged = [row for row in changed if row["change_pct"] == 0]

    def total(group: list[dict], field: str) -> float:
        return sum(float(row[field]) for row in group if row.get(field) is not None)

    up_turnover = total(advances, "turnover")
    down_turnover = total(declines, "turnover")

    history = defaultdict(list)
    for row in db.get_market_close_history(
        "krx", dataset, end=latest["date"], observations=20
    ):
        history[row["symbol"]].append(row["close"])
    eligible = {symbol: values for symbol, values in history.items() if len(values) >= 20}
    above_ma20 = sum(values[-1] > sum(values) / len(values) for values in eligible.values())
    new_high_20 = sum(values[-1] >= max(values) for values in eligible.values())
    new_low_20 = sum(values[-1] <= min(values) for values in eligible.values())

    return {
        "market": label,
        "dataset": dataset,
        "status": "ok",
        "as_of": latest["date"],
        "issues": len(rows),
        "priced_issues": len(changed),
        "advances": len(advances),
        "declines": len(declines),
        "unchanged": len(unchanged),
        "advance_decline_ratio": (
            round(len(advances) / len(declines), 3) if declines else None
        ),
        "advance_share_pct": (
            round(len(advances) / len(changed) * 100, 2) if changed else None
        ),
        "up_turnover": up_turnover,
        "down_turnover": down_turnover,
        "up_down_turnover_ratio": (
            round(up_turnover / down_turnover, 3) if down_turnover else None
        ),
        "top10_market_cap_pct": _concentration(rows, "market_cap"),
        "top10_turnover_pct": _concentration(rows, "turnover"),
        "history_20d": {
            "eligible_issues": len(eligible),
            "above_ma20": above_ma20,
            "above_ma20_pct": (
                round(above_ma20 / len(eligible) * 100, 2) if eligible else None
            ),
            "new_highs": new_high_20,
            "new_lows": new_low_20,
        },
    }


def krx_breadth_snapshot() -> dict:
    """Derive KOSPI/KOSDAQ breadth from cached official KRX stock tables."""
    markets = [
        _breadth_market("stk_bydd_trd", "KOSPI"),
        _breadth_market("ksq_bydd_trd", "KOSDAQ"),
    ]
    dates = [market["as_of"] for market in markets if market["as_of"]]
    return {
        "as_of": max(dates, default=None),
        "markets": markets,
        "source": "krx",
        "cached": True,
        "warning": (
            "등락·거래대금·집중도는 기술 통계입니다. 20일 지표는 최소 20개 "
            "관측치가 있는 종목만 포함합니다."
        ),
    }
