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
    aggregate: str | None = None, history_days: int | None = None,
) -> dict:
    return {
        "dataset": dataset,
        "path": path,
        "label": label,
        "asset_type": asset_type,
        "tier": tier,
        # How deep the second-line layer backfills this table. Per dataset
        # because the cost is per dataset: the option table is ~9,000 rows a
        # session and the derivative index table is ~320, so one shared depth
        # either starves the cheap table or bloats the expensive one. None
        # means the layer's default. This is a collection budget, not part of
        # a target's identity — it must stay out of the recovery fingerprint,
        # or changing it would re-arm every KRX target.
        "history_days": history_days,
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
    _spec("bon_dd_trd", "idx/bon_dd_trd", "채권지수", "bond_index", "balanced",
          aggregate="bond_index", history_days=250),
    # VKOSPI arrives as one row among 320 derivative indices. Promoting it to
    # an indicator series is what makes it addressable by name instead of
    # requiring every consumer to know the row it hides in.
    # Three years of this table is ~240,000 rows and it carries VKOSPI, whose
    # score is taken against its whole record: implied volatility mean
    # reverts, so a baseline that only spans the current crisis would call the
    # crisis normal. Verified served back to 2022. The option table is ~9,000
    # rows a session and stays shallow.
    _spec("drvprod_dd_trd", "idx/drvprod_dd_trd", "파생상품지수", "derivative_index",
          "balanced", aggregate="named_index", history_days=750),
    _spec("stk_bydd_trd", "sto/stk_bydd_trd", "유가증권", "stock", "balanced"),
    _spec("ksq_bydd_trd", "sto/ksq_bydd_trd", "코스닥", "stock", "balanced"),
    _spec("knx_bydd_trd", "sto/knx_bydd_trd", "코넥스", "stock", "balanced"),
    # 하루 1,160행이라 250일이면 29만 행이고, 괴리율 꼬리는 상장·폐지에 따른
    # 구성 변화가 과거로 갈수록 누적됩니다. 앞으로 쌓이게 두는 편이 낫습니다.
    _spec("etf_bydd_trd", "etp/etf_bydd_trd", "ETF", "etf", "light",
          aggregate="etp"),
    _spec("etn_bydd_trd", "etp/etn_bydd_trd", "ETN", "etn", "balanced"),
    _spec("oil_bydd_trd", "gen/oil_bydd_trd", "석유시장", "commodity", "light"),
    _spec("gold_bydd_trd", "gen/gold_bydd_trd", "금시장", "commodity", "light",
          aggregate="gold", history_days=250),
    _spec("ets_bydd_trd", "gen/ets_bydd_trd", "배출권시장", "commodity", "light"),
    _spec("sw_bydd_trd", "sto/sw_bydd_trd", "신주인수권증권", "warrant", "all"),
    _spec("sr_bydd_trd", "sto/sr_bydd_trd", "신주인수권증서", "warrant", "all"),
    _spec("stk_isu_base_info", "sto/stk_isu_base_info", "유가증권 기본정보", "stock", "all"),
    _spec("ksq_isu_base_info", "sto/ksq_isu_base_info", "코스닥 기본정보", "stock", "all"),
    _spec("knx_isu_base_info", "sto/knx_isu_base_info", "코넥스 기본정보", "stock", "all"),
    _spec("elw_bydd_trd", "etp/elw_bydd_trd", "ELW", "elw", "all"),
    _spec("kts_bydd_trd", "bon/kts_bydd_trd", "국채전문유통시장", "bond", "all",
          aggregate="govbond", history_days=250),
    _spec("bnd_bydd_trd", "bon/bnd_bydd_trd", "일반채권시장", "bond", "all"),
    _spec("smb_bydd_trd", "bon/smb_bydd_trd", "소액채권시장", "bond", "all"),
    _spec("fut_bydd_trd", "drv/fut_bydd_trd", "선물", "future", "all",
          aggregate="futures", history_days=250),
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


# A weekday with no rows is usually a holiday, but it is also what a session
# looks like before the exchange publishes it. Treating the two the same made
# an early request permanently retire a trading day: the day was recorded
# empty, every layer then skipped it, and KRX-derived series silently stopped
# a few days behind while looking settled.
EMPTY_SETTLES_AFTER_DAYS = max(1, int(os.environ.get("KRX_EMPTY_SETTLES_DAYS", "7")))


def empty_is_final(day: str, today: date | None = None) -> bool:
    """True once an empty answer for ``day`` can only mean "no session"."""
    try:
        observed = date.fromisoformat(day)
    except (TypeError, ValueError):
        return True
    return ((today or kst_today()) - observed).days >= EMPTY_SETTLES_AFTER_DAYS


def history_dates(count: int, end: str) -> list[str]:
    """``count`` weekdays ending at ``end`` (inclusive), oldest first.

    Separate from ``catchup_dates`` on purpose. That one walks back from today
    and is capped at 20 to protect first-line collection; this one walks back
    from a fixed anchor so raising a dataset's configured depth extends the
    historical generation backwards only, never forwards.
    """
    cursor = date.fromisoformat(end)
    days: list[str] = []
    while len(days) < max(1, count):
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return list(reversed(days))


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


PUT_CALL_MEASURES = ("volume", "value", "open_interest")

# KRX lists a night session alongside the day session for several products.
# Counting both double-counts the same market, so every aggregate that sums
# contracts excludes it by the same rule rather than each re-deriving one.
def _is_night_session(row: dict) -> bool:
    return "야간" in str(row.get("ISU_NM", ""))


def _maturity_token(name: str) -> str | None:
    """The YYMM maturity code inside a KTB issue name.

    ``국고04250-3606(26-6)`` matures 2036-06; the linker ``물가01125-3606``
    matures the same month. Pairing on this token is what makes a breakeven
    a like-for-like comparison instead of two different horizons subtracted.
    """
    head = name.split("(")[0]
    return head.split("-")[-1].strip() if "-" in head else None


def aggregate_indicator_keys(spec: dict) -> set[str]:
    """Which indicator series this dataset's aggregate can produce.

    Lets an audit ask "does this stored day already have its derived series?"
    without parsing the day's rows first.
    """
    kind = spec.get("aggregate")
    if kind == "named_index":
        return set(NAMED_INDICES.values())
    return set(AGGREGATE_KEYS.get(kind) or ())


def aggregate_govbond(rows: list[dict], day: str) -> list[dict]:
    """Series the exchange's government bond table implies but does not print.

    Only 지표종목 (on-the-run) are used. The table's maturity field is the
    bond's *original* maturity, so an off-the-run 10-year with one year left
    still says 10; mixing those in produces a curve that never existed.
    """
    on_the_run = [
        row for row in rows if str(row.get("GOVBND_ISU_TP_NM", "")).strip() == "지표"
    ]
    points = []
    twenty = [
        _number(row.get("CLSPRC_YD")) for row in on_the_run
        if str(row.get("BND_EXP_TP_NM", "")).strip() == "20"
        and not str(row.get("ISU_NM", "")).startswith("물가")
    ]
    if len(twenty) == 1 and twenty[0] is not None:
        points.append({
            "indicator": "kr_treasury_20y",
            "date": _iso_day(day),
            "value": twenty[0],
        })
    # A breakeven is only a breakeven when both legs mature together. The
    # benchmark linker and the benchmark nominal do not always share a
    # maturity month — when the linker was 물가00750-3406 the on-the-run
    # nominal matured 2035-06, and no bond at 2034-06 was quoted at all. Days
    # like that have no observation rather than a nearby maturity subtracted
    # and called a 10-year breakeven.
    linkers = [r for r in on_the_run if str(r.get("ISU_NM", "")).startswith("물가")]
    for linker in linkers:
        token = _maturity_token(str(linker.get("ISU_NM", "")))
        nominals = [
            row for row in on_the_run
            if not str(row.get("ISU_NM", "")).startswith("물가")
            and _maturity_token(str(row.get("ISU_NM", ""))) == token
        ]
        if token is None or len(nominals) != 1:
            continue
        real = _number(linker.get("CLSPRC_YD"))
        nominal = _number(nominals[0].get("CLSPRC_YD"))
        if real is None or nominal is None:
            continue
        points.append({
            "indicator": "kr_breakeven_10y",
            "date": _iso_day(day),
            "value": round(nominal - real, 3),
        })
        break
    return points


def aggregate_bond_index(rows: list[dict], day: str) -> list[dict]:
    """Average duration of the broad KRX bond index — the market's rate risk."""
    for row in rows:
        if str(row.get("BND_IDX_GRP_NM", "")).strip() != "KRX 채권지수":
            continue
        value = _number(row.get("AVG_DURATION"))
        if value is None:
            continue
        return [{
            "indicator": "kr_bond_duration",
            "date": _iso_day(day),
            "value": value,
        }]
    return []


def aggregate_futures(rows: list[dict], day: str) -> list[dict]:
    """KOSPI200 futures positioning: open interest and the near-month basis."""
    contracts = [
        row for row in rows
        if row.get("PROD_NM") == "코스피200 선물" and not _is_night_session(row)
    ]
    if not contracts:
        return []
    points = []
    total = sum(_number(row.get("ACC_OPNINT_QTY")) or 0 for row in contracts)
    if total:
        points.append({
            "indicator": "kr_kospi200_futures_oi",
            "date": _iso_day(day),
            "value": total,
        })
    # The near month is where the open interest is, not where the name sorts:
    # through a rollover the expiring month still sorts first while liquidity
    # has already moved on.
    near = max(contracts, key=lambda row: _number(row.get("ACC_OPNINT_QTY")) or 0)
    close, spot = _number(near.get("TDD_CLSPRC")), _number(near.get("SPOT_PRC"))
    if close and spot:
        points.append({
            "indicator": "kr_kospi200_basis",
            "date": _iso_day(day),
            "value": round((close - spot) / spot * 100, 4),
        })
    return points


def aggregate_etp(rows: list[dict], day: str) -> list[dict]:
    """The stressed tail of ETF price-to-NAV, as a positive discount.

    Stored so that a rise means stress, which is what the direction
    declaration expects. Funds that did not trade are excluded: a stale quote
    against a moving NAV produces a gap nobody could have transacted at.
    """
    discounts = []
    for row in rows:
        if not (_number(row.get("ACC_TRDVOL")) or 0):
            continue
        price, nav = _number(row.get("TDD_CLSPRC")), _number(row.get("NAV"))
        if not price or not nav or price <= 0 or nav <= 0:
            continue
        discounts.append((price / nav - 1) * 100)
    if len(discounts) < 100:
        return []
    discounts.sort()
    tail = discounts[len(discounts) // 20]
    return [{
        "indicator": "kr_etf_discount",
        "date": _iso_day(day),
        "value": round(-tail, 4),
    }]


def aggregate_gold(rows: list[dict], day: str) -> list[dict]:
    """Domestic gold price from the KRX gold market, in won per gram."""
    for row in rows:
        # The provider has written both "1Kg" and "1kg" for the same bar;
        # matching case-sensitively silently dropped four months of history.
        if "99.99_1kg" not in str(row.get("ISU_NM", "")).lower():
            continue
        value = _number(row.get("TDD_CLSPRC"))
        if value is None:
            continue
        return [{
            "indicator": "kr_gold_price",
            "date": _iso_day(day),
            "value": value,
        }]
    return []




def derive_aggregate_points(spec: dict, rows: list[dict], day: str) -> list[dict]:
    """Market-wide series a bulk table implies but no single row carries.

    Both collection layers derive these, so the dispatch lives here rather
    than in either caller: a layer that stored the rows without the series it
    implies would leave the table complete and the gauge empty, which is
    exactly how VKOSPI ended up with five observations behind twenty days of
    stored rows.
    """
    # Dispatched by name rather than through a table of function objects: a
    # table binds whatever the function was at import time, so a replaced or
    # patched implementation would be silently bypassed.
    kind = spec.get("aggregate")
    if kind == "put_call":
        return aggregate_put_call(rows, day)
    if kind == "named_index":
        return extract_named_indices(rows, day)
    if kind == "govbond":
        return aggregate_govbond(rows, day)
    if kind == "bond_index":
        return aggregate_bond_index(rows, day)
    if kind == "futures":
        return aggregate_futures(rows, day)
    if kind == "etp":
        return aggregate_etp(rows, day)
    if kind == "gold":
        return aggregate_gold(rows, day)
    return []


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
        if _is_night_session(row):
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

AGGREGATE_KEYS = {
    "put_call": {f"kr_put_call_{measure}" for measure in PUT_CALL_MEASURES},
    "named_index": None,          # resolved from NAMED_INDICES
    "govbond": {"kr_treasury_20y", "kr_breakeven_10y"},
    "bond_index": {"kr_bond_duration"},
    "futures": {"kr_kospi200_futures_oi", "kr_kospi200_basis"},
    "etp": {"kr_etf_discount"},
    "gold": {"kr_gold_price"},
}

