"""Provider-wide KRX Open API collection with bounded resource usage.

KRX daily endpoints return every row for one market and date.  This collector
therefore discovers instruments from the response instead of maintaining a
symbol allowlist.  The default ``balanced`` scope keeps broad cash-market
coverage while leaving high-volume specialist products behind an opt-in.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, timedelta

import httpx

from app.timeutil import kst_today


BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"


def _spec(
    dataset: str, path: str, label: str, asset_type: str, tier: str,
    aggregate: str | None = None,
) -> dict:
    return {
        "dataset": dataset,
        "path": path,
        "label": label,
        "asset_type": asset_type,
        "tier": tier,
        # Some provider tables also yield a market-wide statistic that no
        # single row carries.  The tag adds a summary alongside the rows; it
        # never replaces them, because a contract we discard today cannot be
        # reconstructed from an aggregate tomorrow.
        "aggregate": aggregate,
    }


# KRX's official service catalog currently exposes 31 daily/as-of-date tables.
# ``light`` is included in ``balanced``, and both are included in ``all``.
DATASETS = [
    _spec("krx_dd_trd", "idx/krx_dd_trd", "KRX 시리즈 지수", "index", "light"),
    _spec("kospi_dd_trd", "idx/kospi_dd_trd", "KOSPI 시리즈 지수", "index", "light"),
    _spec("kosdaq_dd_trd", "idx/kosdaq_dd_trd", "KOSDAQ 시리즈 지수", "index", "light"),
    _spec("bon_dd_trd", "idx/bon_dd_trd", "채권지수", "bond_index", "balanced"),
    # VKOSPI arrives as one row among 320 derivative indices. Promoting it to
    # an indicator series is what makes it addressable by name instead of
    # requiring every consumer to know the row it hides in.
    _spec("drvprod_dd_trd", "idx/drvprod_dd_trd", "파생상품지수", "derivative_index",
          "balanced", aggregate="named_index"),
    _spec("stk_bydd_trd", "sto/stk_bydd_trd", "유가증권", "stock", "balanced"),
    _spec("ksq_bydd_trd", "sto/ksq_bydd_trd", "코스닥", "stock", "balanced"),
    _spec("knx_bydd_trd", "sto/knx_bydd_trd", "코넥스", "stock", "balanced"),
    _spec("etf_bydd_trd", "etp/etf_bydd_trd", "ETF", "etf", "light"),
    _spec("etn_bydd_trd", "etp/etn_bydd_trd", "ETN", "etn", "balanced"),
    _spec("oil_bydd_trd", "gen/oil_bydd_trd", "석유시장", "commodity", "light"),
    _spec("gold_bydd_trd", "gen/gold_bydd_trd", "금시장", "commodity", "light"),
    _spec("ets_bydd_trd", "gen/ets_bydd_trd", "배출권시장", "commodity", "light"),
    _spec("sw_bydd_trd", "sto/sw_bydd_trd", "신주인수권증권", "warrant", "all"),
    _spec("sr_bydd_trd", "sto/sr_bydd_trd", "신주인수권증서", "warrant", "all"),
    _spec("stk_isu_base_info", "sto/stk_isu_base_info", "유가증권 기본정보", "stock", "all"),
    _spec("ksq_isu_base_info", "sto/ksq_isu_base_info", "코스닥 기본정보", "stock", "all"),
    _spec("knx_isu_base_info", "sto/knx_isu_base_info", "코넥스 기본정보", "stock", "all"),
    _spec("elw_bydd_trd", "etp/elw_bydd_trd", "ELW", "elw", "all"),
    _spec("kts_bydd_trd", "bon/kts_bydd_trd", "국채전문유통시장", "bond", "all"),
    _spec("bnd_bydd_trd", "bon/bnd_bydd_trd", "일반채권시장", "bond", "all"),
    _spec("smb_bydd_trd", "bon/smb_bydd_trd", "소액채권시장", "bond", "all"),
    _spec("fut_bydd_trd", "drv/fut_bydd_trd", "선물", "future", "all"),
    _spec("eqsfu_stk_bydd_trd", "drv/eqsfu_stk_bydd_trd", "유가 주식선물", "future", "all"),
    _spec("eqkfu_ksq_bydd_trd", "drv/eqkfu_ksq_bydd_trd", "코스닥 주식선물", "future", "all"),
    # Roughly 17,000 contracts a day, about a tenth of which trade.  Every
    # row is kept — strike-level history is not recoverable after the fact and
    # implied volatility surfaces need it — and the put/call ratios are
    # derived alongside so the dashboard does not re-scan the table.
    _spec("opt_bydd_trd", "drv/opt_bydd_trd", "옵션", "option", "balanced",
          aggregate="put_call"),
    _spec("eqsop_bydd_trd", "drv/eqsop_bydd_trd", "유가 주식옵션", "option", "all"),
    _spec("eqkop_bydd_trd", "drv/eqkop_bydd_trd", "코스닥 주식옵션", "option", "all"),
    _spec("esg_etp_info", "esg/esg_etp_info", "ESG 증권상품", "esg_product", "all"),
    _spec("sri_bond_info", "esg/sri_bond_info", "사회책임투자채권", "esg_bond", "all"),
    _spec("esg_index_info", "esg/esg_index_info", "ESG 지수", "esg_index", "all"),
]


def enabled() -> bool:
    value = os.environ.get("KRX_MARKET_ENABLED", "auto").strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    return bool(os.environ.get("KRX_API_KEY", "").strip())


def dataset_specs(scope: str | None = None) -> list[dict]:
    override = os.environ.get("KRX_MARKET_DATASETS", "").strip()
    known = {spec["dataset"]: spec for spec in DATASETS}
    if override:
        requested = [item.strip() for item in override.split(",") if item.strip()]
        unknown = sorted(set(requested) - set(known))
        if unknown:
            raise ValueError(f"unknown KRX datasets: {', '.join(unknown)}")
        return [known[item].copy() for item in requested]

    scope = (scope or os.environ.get("KRX_MARKET_SCOPE", "balanced")).strip().lower()
    if scope not in {"light", "balanced", "all"}:
        raise ValueError("KRX_MARKET_SCOPE must be light, balanced, or all")
    allowed = {
        "light": {"light"},
        "balanced": {"light", "balanced"},
        "all": {"light", "balanced", "all"},
    }[scope]
    return [spec.copy() for spec in DATASETS if spec["tier"] in allowed]


def catchup_dates(count: int | None = None, today: date | None = None) -> list[str]:
    """Return recent weekdays before today, oldest first.

    KRX EOD Open API data is generally available after the trading date.  Empty
    weekday responses (public holidays) are recorded so they are not retried.
    """
    count = count or int(os.environ.get("KRX_CATCHUP_BUSINESS_DAYS", "5"))
    count = max(1, min(count, 20))
    cursor = (today or kst_today()) - timedelta(days=1)
    days: list[str] = []
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return list(reversed(days))


def fetch_dataset(spec: dict, day: str) -> list[dict]:
    key = os.environ.get("KRX_API_KEY", "").strip()
    if not key:
        raise RuntimeError("KRX_API_KEY is not configured")
    response = httpx.get(
        f"{BASE_URL}/{spec['path']}",
        params={"basDd": day.replace("-", "")},
        headers={"AUTH_KEY": key, "Accept": "application/json"},
        timeout=40,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"KRX {spec['dataset']} returned non-JSON HTTP {response.status_code}"
        ) from exc
    if response.status_code == 401 or str(payload.get("respCode", "")) == "401":
        raise RuntimeError(
            f"KRX {spec['dataset']} 인증 실패 또는 서비스 미승인 (HTTP 401)"
        )
    response.raise_for_status()
    if payload.get("respCode") not in (None, "", "200", 200):
        raise RuntimeError(
            f"KRX {spec['dataset']}: {payload.get('respMsg') or payload['respCode']}"
        )
    rows = payload.get("OutBlock_1") or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"KRX {spec['dataset']} returned an invalid row set")
    # The option table alone returns ~18,000 contracts on a normal day and
    # more when weeklies cluster, so the guard sits well above it. It exists
    # to catch a provider returning something unexpected, not to ration
    # normal collection.
    maximum = max(1, int(os.environ.get("KRX_MAX_ROWS_PER_DATASET", "60000")))
    if len(rows) > maximum:
        raise RuntimeError(
            f"KRX {spec['dataset']} row budget exceeded: {len(rows)} > {maximum}"
        )
    return rows


def _number(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    if text in {"", "-", ".", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _first(row: dict, *keys: str):
    for key in keys:
        value = row.get(key)
        if value not in (None, "", "-"):
            return value
    return None


def _iso_day(value: str) -> str:
    value = str(value)
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    date.fromisoformat(value)
    return value


def normalize_row(spec: dict, row: dict, requested_day: str) -> dict:
    day = _iso_day(_first(row, "BAS_DD") or requested_day)
    symbol = _first(
        row,
        "ISU_SRT_CD",
        "ISU_CD",
        "IDX_NM",
        "BND_IDX_GRP_NM",
        "OIL_NM",
        "ISU_ABBRV",
        "ISUR_NM",
    )
    if symbol is None:
        identity = {
            key: value for key, value in row.items()
            if key != "BAS_DD" and not any(
                token in key for token in ("PRC", "IDX", "VOL", "VAL", "AMT", "RATE")
            )
        }
        digest = hashlib.sha256(
            json.dumps(identity or row, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:16]
        symbol = f"row:{digest}"
    name = str(_first(
        row, "ISU_ABBRV", "ISU_NM", "IDX_NM", "BND_IDX_GRP_NM", "OIL_NM", "ISUR_NM"
    ) or symbol)
    return {
        "symbol": str(symbol),
        "name": name,
        "asset_type": spec["asset_type"],
        "market": str(_first(row, "MKT_NM", "IDX_CLSS") or spec["label"]),
        "currency": "KRW",
        "date": day,
        "close": _number(_first(
            row, "TDD_CLSPRC", "CLSPRC_IDX", "CLSPRC", "TOT_EARNG_IDX",
            "WT_AVG_PRC", "SETL_PRC"
        )),
        "change": _number(_first(
            row, "CMPPREVDD_PRC", "CMPPREVDD_IDX", "TOT_EARNG_IDX_CMPPREVDD"
        )),
        "change_pct": _number(_first(row, "FLUC_RT", "UPDN_RATE")),
        "open": _number(_first(row, "TDD_OPNPRC", "OPNPRC_IDX", "OPNPRC")),
        "high": _number(_first(row, "TDD_HGPRC", "HGPRC_IDX", "HGPRC")),
        "low": _number(_first(row, "TDD_LWPRC", "LWPRC_IDX", "LWPRC")),
        "volume": _number(_first(row, "ACC_TRDVOL")),
        "turnover": _number(_first(row, "ACC_TRDVAL")),
        "market_cap": _number(_first(row, "MKTCAP")),
        "metadata": {
            "provider_path": spec["path"],
            "dataset_label": spec["label"],
            # Option contracts carry identity the generic columns have no
            # place for. Promoting it out of the raw blob makes strike-level
            # queries possible without parsing JSON per row.
            **({
                "right": row["RGHT_TP_NM"],
                "product": row.get("PROD_NM"),
                "implied_volatility": _number(row.get("IMP_VOLT")),
                "open_interest": _number(row.get("ACC_OPNINT_QTY")),
            } if row.get("RGHT_TP_NM") else {}),
        },
        "raw": row,
    }


def normalize_rows(spec: dict, rows: list[dict], day: str) -> list[dict]:
    return [normalize_row(spec, row, day) for row in rows]


# The put/call ratio is read off index options.  Individual stock options are
# a different table and far too thin to carry a market-wide signal, and the
# overnight session is excluded so the ratio describes one regular session.
PUT_CALL_PRODUCTS = ("코스피200 옵션", "코스피200 위클리(목) 옵션", "코스피200 위클리(월) 옵션")


# Provider index names mapped to the catalog keys that address them.
NAMED_INDICES = {
    "코스피 200 변동성지수": "kr_vkospi",
}


def extract_named_indices(rows: list[dict], day: str) -> list[dict]:
    """Lift individually useful indices out of a bulk index table."""
    points = []
    for row in rows:
        key = NAMED_INDICES.get(str(row.get("IDX_NM", "")).strip())
        if not key:
            continue
        value = _number(_first(row, "CLSPRC_IDX", "TDD_CLSPRC"))
        if value is None:
            continue
        points.append({"indicator": key, "date": _iso_day(day), "value": value})
    return points


def aggregate_put_call(rows: list[dict], day: str) -> list[dict]:
    """Derive the day's put/call ratios from the stored option contracts.

    Returns indicator points, not instruments: a put/call ratio describes the
    market's positioning, not a tradable thing with its own price history.
    The contracts themselves are stored separately and in full.
    """
    totals = {
        "volume": {"CALL": 0.0, "PUT": 0.0},
        "value": {"CALL": 0.0, "PUT": 0.0},
        "open_interest": {"CALL": 0.0, "PUT": 0.0},
    }
    fields = {
        "volume": "ACC_TRDVOL",
        "value": "ACC_TRDVAL",
        "open_interest": "ACC_OPNINT_QTY",
    }
    for row in rows:
        if row.get("PROD_NM") not in PUT_CALL_PRODUCTS:
            continue
        if "야간" in str(row.get("ISU_NM", "")):
            continue
        side = str(row.get("RGHT_TP_NM", "")).upper()
        if side not in ("CALL", "PUT"):
            continue
        for measure, field in fields.items():
            value = _number(row.get(field))
            if value:
                totals[measure][side] += value
    points = []
    for measure, sides in totals.items():
        calls, puts = sides["CALL"], sides["PUT"]
        if calls <= 0:
            continue
        points.append({
            "indicator": f"kr_put_call_{measure}",
            "date": _iso_day(day),
            "value": round(puts / calls, 6),
        })
    return points
