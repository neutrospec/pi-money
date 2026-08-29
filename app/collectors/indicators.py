"""Indicator time-series collectors: FRED (US) + ECOS (Korea).

Each returns a list of {"date": "YYYY-MM-DD", "value": float} sorted ascending.
Collectors are independent so one failing source doesn't break the others.
"""
from __future__ import annotations

import datetime as dt
import os
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import httpx
from dotenv import load_dotenv

from app.timeutil import kst_today


load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# --------------------------------------------------------------------------
# FRED (US) — official, free
# --------------------------------------------------------------------------
FRED_BASE = "https://api.stlouisfed.org"

# Frequency is source metadata, not collector control flow.  Keeping it next
# to the provider series ID makes catalog additions declarative and prevents a
# new key from silently falling through to the wrong scheduler.
FRED_FREQUENCIES = {
    **dict.fromkeys((
        "SOFR", "DGS3MO", "DGS1", "DGS2", "DGS5", "DGS10", "DGS30",
        "DFII10", "T10YIE", "THREEFYTP10", "T5YIFR", "RRPONTSYD",
        "BAMLC0A0CM", "BAMLC0A4CBBB", "BAMLH0A0HYM2", "VIXCLS",
        "DEXKOUS", "DTWEXBGS", "ECBDFR", "DEXUSEU", "DEXJPUS",
        "DEXCHUS", "DEXBZUS", "DEXUSUK",
    ), "D"),
    **dict.fromkeys((
        "ICSA", "CCSA", "NFCI", "STLFSI4", "WALCL", "WRESBAL", "WTREGEN",
    ), "W"),
    **dict.fromkeys((
        "CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE", "PPIACO", "FEDFUNDS",
        "UNRATE", "PAYEMS", "JTSJOL", "JTSQUR", "AWHMAN", "INDPRO",
        "RRSFS", "HOUST", "CSUSHPINSA", "DGORDER", "M2SL", "UMCSENT",
        "CFNAI", "SAHMREALTIME", "CP0000EZ19M086NEST", "IR3TIB01JPM156N",
        "CHNCPIALLMINMEI", "CPALTT01GBM657N", "IR3TIB01GBM156N",
        "IR3TIB01EZM156N",
    ), "M"),
    **dict.fromkeys((
        "GDPC1", "DRTSCILM", "CLVMNACSCAB1GQEA19", "LRHUTTTTJPQ156S",
    ), "Q"),
    **dict.fromkeys(("GBRCPIALLMINMEI", "IRSTCI01JPM156N"), "M"),
}

DEFAULT_MAX_AGE_DAYS = {"D": 5, "W": 14, "M": 100, "Q": 160, "A": 550}

# Daily series divide into two populations that a coverage audit must not
# treat alike.  The test is what the *provider publishes*, not whether the
# rate is conceptually in force: the Bank of England's Bank Rate applies on a
# Sunday too, but the BoE only publishes it on business days, so expecting a
# Sunday observation invents hundreds of gaps that were never missing.  Each
# entry below was confirmed against the stored weekday distribution.
# Series that are not daily are period-stamped and audited by period instead.
CALENDAR_DAY_SERIES = {
    ("ecos", "rate.base"),          # ECOS는 주말 포함 매일 게시
    ("fred", "ECBDFR"),             # FRED는 주말 포함 매일 게시
}
SOURCE_FRESHNESS_OVERRIDES = {
    # The New York Fed ACM term-premium estimate is a daily-frequency series
    # published with a longer operational lag than market yields.
    ("fred", "THREEFYTP10"): 14,
    # Verified provider-latest series whose observation date is the period
    # start; the allowance describes publication lag rather than hiding gaps.
    ("fred", "CCSA"): 21,
    ("fred", "CSUSHPINSA"): 130,
    ("ecos", "consumption.credit_card_spending"): 130,
    ("ecos", "employment.hourly_wage"): 250,
    ("ecos", "employment.unit_labor_cost"): 250,
    # OECD MEI 경유 계열: 공급자가 2025년 중반 이후 갱신을 멈췄습니다. 허용치는
    # 로컬 결측을 감추는 것이 아니라 관측된 공급 현실을 표현합니다. 각국 원천
    # (ONS·NBS) 직접 파서는 별도 작업으로 남겨둡니다.
    ("fred", "GBRCPIALLMINMEI"): 600,
    ("fred", "CHNCPIALLMINMEI"): 600,
    ("fred", "IRSTCI01JPM156N"): 130,
    # H.10 환율은 일별 관측이지만 공표는 주 1회(월요일, 직전 금요일까지)입니다.
    # 따라서 정상 상태에서도 관측일 기준 9~10일까지 벌어지며, 휴일이 끼면 더
    # 늘어납니다. 5일 허용치는 매주 수요일부터 월요일까지 이 계열들을 결측으로
    # 오판해 복구 루프가 같은 값을 6시간마다 다시 받아오게 만들었습니다.
    ("fred", "DEXUSEU"): 14,
    ("fred", "DTWEXBGS"): 14,
    ("fred", "DEXKOUS"): 14,
    ("fred", "DEXBZUS"): 14,
    ("fred", "DEXCHUS"): 14,
    ("fred", "DEXUSUK"): 14,
    ("fred", "DEXJPUS"): 14,
}

# Some curated ECOS paths identify a table subtotal but leave a provider
# dimension open.  The selected variant is explicit provenance and is applied
# before date normalization so multiple same-date rows can never overwrite one
# another by iteration order.
ECOS_SELECTORS = {
    "credit.household_delinquency_rate": {"item_code2": "X00"},  # 전국
    "sentiment.business": {"item_code2": "AX"},           # 기업심리지수 실적
    "production.manufacturing.inventory": {"item_code2": "6"},  # 계절조정 재고
    "production.manufacturing.utilization": {"item_code2": "I11C"},
    "consumption.retail_sales": {"item_code2": "T3"},     # 계절조정지수
    "trade.exports.semiconductor.price": {"item_code2": "W"},  # 원화기준
    "employment.unemployment_rate": {"item_code2": "I28B"},   # 계절조정
    "production.all_industry": {"item_code2": "2"},       # 계절조정
    "production.services": {"item_code2": "3"},           # 계절조정지수
    "investment.construction_started": {"item_code2": "I47AA"},  # 자재별 총계
    "employment.employment_rate": {"item_code2": "I28B"}, # 계절조정
    "trade.imports.price": {"item_code2": "W"},           # 원화기준
}


def _fred_key() -> str:
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FRED_API_KEY is not configured")
    return key


def fred_series(series_id: str, years: int = 3) -> list[dict]:
    """Fetch a FRED series over the last N years."""
    start = (kst_today() - timedelta(days=365 * years)).isoformat()
    r = httpx.get(
        f"{FRED_BASE}/fred/series/observations",
        params={
            "series_id": series_id,
            "api_key": _fred_key(),
            "file_type": "json",
            "observation_start": start,
        },
        timeout=20,
    )
    r.raise_for_status()
    obs = r.json().get("observations", [])
    out = []
    for o in obs:
        v = o.get("value")
        if v == ".":  # FRED missing marker
            continue
        try:
            out.append({"date": o["date"], "value": float(v)})
        except (ValueError, TypeError):
            continue
    return out


# --------------------------------------------------------------------------
# ECOS raw statistic tables — series pyecos does not curate
# --------------------------------------------------------------------------
# pyecos exposes a curated subset that omits most of the Korean money market:
# no KOFR, no CP, no government bond beyond five years.  Those are the series
# this project exists to watch, so they are addressed by their provider table
# and item code directly.  Format: "STAT_CODE/ITEM_CODE".
ECOS_RAW_BASE = "https://ecos.bok.or.kr/api"
ECOS_RAW_FREQUENCIES = {
    "817Y002": "D",   # 시장금리 (일별)
}
ECB_FREQUENCIES = {
    "FM/M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA": "M",
}
BOE_FREQUENCIES = {
    "IUDBEDR": "D",   # Bank Rate
    "IUDSOIA": "D",   # SONIA overnight
}


def _ecos_key() -> str:
    key = os.environ.get("ECOS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ECOS_API_KEY is not configured")
    return key


def ecos_raw_series(series: str, years: int = 3) -> list[dict]:
    """Fetch one item from an ECOS statistic table by explicit code."""
    stat_code, item_code = series.split("/", 1)
    cycle = ECOS_RAW_FREQUENCIES.get(stat_code)
    if cycle != "D":
        raise ValueError(f"unsupported ECOS raw cycle for {stat_code}: {cycle}")
    today = kst_today()
    start = (today - timedelta(days=365 * years)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    response = httpx.get(
        f"{ECOS_RAW_BASE}/StatisticSearch/{_ecos_key()}/json/kr/1/10000/"
        f"{stat_code}/{cycle}/{start}/{end}/{item_code}",
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if "StatisticSearch" not in payload:
        message = payload.get("RESULT", {}).get("MESSAGE") or str(payload)[:200]
        raise RuntimeError(f"ECOS returned no result for {series}: {message}")
    out = []
    for row in payload["StatisticSearch"].get("row", []):
        stamp = str(row.get("TIME", ""))
        if len(stamp) != 8:
            continue
        try:
            value = float(row["DATA_VALUE"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}", "value": value})
    return out


# --------------------------------------------------------------------------
# ECB Data Portal and Bank of England — official replacements for series that
# reach FRED via OECD and stall there for months at a time.
# --------------------------------------------------------------------------
def ecb_series(series: str, years: int = 3) -> list[dict]:
    """Fetch one ECB Data Portal series. Free, no key, SDMX-JSON."""
    response = httpx.get(
        f"https://data-api.ecb.europa.eu/service/data/{series}",
        params={"format": "jsondata", "lastNObservations": str(years * 13)},
        headers={"User-Agent": "money-market-intelligence/0.2"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    periods = payload["structure"]["dimensions"]["observation"][0]["values"]
    datasets = payload.get("dataSets") or []
    series_map = datasets[0].get("series") if datasets else None
    if not series_map:
        raise ValueError(f"ECB returned no observations for {series}")
    observations = next(iter(series_map.values()))["observations"]
    out = []
    for index, values in observations.items():
        period = periods[int(index)]["id"]
        if values[0] is None:
            continue
        if len(period) == 7:              # YYYY-MM
            date_value = f"{period}-01"
        elif len(period) == 10:           # YYYY-MM-DD
            date_value = period
        else:
            continue
        out.append({"date": date_value, "value": float(values[0])})
    return sorted(out, key=lambda point: point["date"])


def boe_series(series: str, years: int = 3) -> list[dict]:
    """Fetch one Bank of England IADB series. Free, no key, CSV."""
    start = (kst_today() - timedelta(days=365 * years)).strftime("%d/%b/%Y")
    response = httpx.get(
        "https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp",
        params={
            "csv.x": "yes", "Datefrom": start, "Dateto": "now",
            "SeriesCodes": series, "CSVF": "TN", "UsingCodes": "Y",
            "VPD": "Y", "VFD": "N",
        },
        headers={"User-Agent": "money-market-intelligence/0.2"},
        timeout=30,
        follow_redirects=True,
    )
    response.raise_for_status()
    lines = [line for line in response.text.strip().splitlines() if line.strip()]
    out = []
    for line in lines[1:]:               # first row is the header
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            stamp = dt.datetime.strptime(parts[0].strip(), "%d %b %Y").date()
            out.append({"date": stamp.isoformat(), "value": float(parts[1])})
        except (ValueError, TypeError):
            continue
    if not out:
        raise ValueError(f"Bank of England returned no observations for {series}")
    return sorted(out, key=lambda point: point["date"])


# --------------------------------------------------------------------------
# ECOS (Korea) — via pyecos
# --------------------------------------------------------------------------
def ecos_series(
    name: str,
    days: int = 365 * 3,
    selectors: dict[str, str] | None = None,
) -> list[dict]:
    """Fetch an ECOS curated series (e.g. 'rate.base', 'fx.usd').

    Handles cycle-based date formats: D=YYYYMMDD, M=YYYYMM, Q=YYYYQ#, A=YYYY.
    """
    from pyecos import ECOS

    key = os.environ.get("ECOS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ECOS_API_KEY is not configured")
    e = ECOS(key)
    try:
        node = e
        for part in name.split("."):
            node = getattr(node, part)
        cycle = node.spec.cycle
        today = kst_today()
        if cycle == "D":
            start = (today - timedelta(days=days)).strftime("%Y%m%d")
            end = today.strftime("%Y%m%d")
        elif cycle == "M":
            start = (today - timedelta(days=days)).strftime("%Y%m")
            end = today.strftime("%Y%m")
        elif cycle == "Q":
            start = (today - timedelta(days=days)).strftime("%Y") + "Q1"
            end = today.strftime("%Y") + f"Q{(today.month - 1) // 3 + 1}"
        else:  # A
            start = (today.year - 3).__str__()
            end = today.year.__str__()
        rows = node.fetch(start=start, end=end)
    finally:
        e.close()
    selectors = selectors or {}
    if selectors:
        rows = [
            row for row in rows
            if all(str(row.get(field, "")) == value for field, value in selectors.items())
        ]
    out = []
    seen_dates: set[str] = set()
    for r in rows:
        t = str(r.get("time", ""))
        if len(t) == 6 and t[4] == "Q":  # quarterly 2026Q3
            q = int(t[5])
            if q not in (1, 2, 3, 4):
                continue
            month = (q - 1) * 3 + 1
            d = f"{t[:4]}-{month:02d}-01"
        elif len(t) == 8:
            d = f"{t[:4]}-{t[4:6]}-{t[6:]}"
        elif len(t) == 6:
            d = f"{t[:4]}-{t[4:6]}-01"
        elif len(t) == 4:
            d = f"{t}-01-01"
        else:
            continue
        try:
            value = float(r["data_value"])
        except (ValueError, TypeError, KeyError):
            continue
        if d in seen_dates:
            raise ValueError(
                f"{name} returned multiple observations for {d}; "
                "declare an ECOS selector"
            )
        seen_dates.add(d)
        out.append({"date": d, "value": value})
    return out


# --------------------------------------------------------------------------
# Analysis coverage metadata
# --------------------------------------------------------------------------
CATEGORY_ANALYSIS_GROUP = {
    "금리": "policy_rates",
    "환율": "fx_external",
    "물가": "inflation",
    "통화": "liquidity",
    "신용": "credit_stress",
    "고용": "labor",
    "성장": "growth_cycle",
    "투자": "growth_cycle",
    "심리": "sentiment",
    "상품": "commodities",
    "시장": "market_breadth",
    "부동산": "housing",
    "대외": "fx_external",
}

KEY_ANALYSIS_GROUP = {
    "kr_put_call_volume": "positioning",
    "kr_put_call_value": "positioning",
    "kr_put_call_open_interest": "positioning",
    "kr_vkospi": "volatility",
    "kr_breakeven_10y": "inflation",
    "kr_treasury_20y": "policy_rates",
    "kr_bond_duration": "valuation",
    "kr_kospi200_futures_oi": "positioning",
    "kr_kospi200_basis": "positioning",
    "kr_etf_discount": "credit_stress",
    "kr_gold_price": "commodities",
    "kr_kospi_per": "valuation",
    "kr_kospi_dividend_yield": "valuation",
    "kr_semiconductor_export_value": "trade_semiconductors",
    "kr_semiconductor_export_volume": "trade_semiconductors",
    "kr_semiconductor_export_price": "trade_semiconductors",
    "kr_dram_ppi": "trade_semiconductors",
    "kr_nand_ppi": "trade_semiconductors",
    "us_sofr": "liquidity",
    "us_real_10y": "policy_rates",
    "us_breakeven_10y": "inflation",
    "us_fed_assets": "liquidity",
    "us_nfci": "credit_stress",
    "us_financial_stress": "credit_stress",
    "us_equal_weight_proxy": "market_breadth",
    "us_semiconductor_proxy": "market_breadth",
    "us_high_yield_proxy": "credit_stress",
    "us_long_treasury_proxy": "policy_rates",
    "us_term_premium_10y": "policy_rates",
    "us_forward_inflation_5y5y": "inflation",
    "us_reserve_balances": "liquidity",
    "us_tga": "liquidity",
    "us_on_rrp": "liquidity",
    "us_manufacturing_hours": "growth_cycle",
    "us_lqd_proxy": "credit_stress",
    "us_regional_bank_proxy": "market_breadth",
    "us_discretionary_proxy": "market_breadth",
    "us_staples_proxy": "market_breadth",
}

CORE_KEYS = {
    # 한국 물가 계열이 전부 실현치(후행)뿐이라, 시장이 앞을 어떻게 보는지
    # 알려주는 유일한 계열입니다. 관측 수가 아직 적어도 채우는 층이 다릅니다.
    "kr_breakeven_10y",
    "kr_base_rate", "kr_call", "kr_kofr", "kr_cd_91d", "kr_cp_91d",
    "kr_treasury_3y", "kr_treasury_10y", "kr_corp_bond_3y",
    "kr_usd", "kr_cpi", "kr_leading_cycle", "kr_manufacturing_output",
    "kr_consumer_sentiment", "kr_fx_reserves", "kr_semiconductor_export_value",
    "kr_dram_ppi", "us_rate", "us_sofr", "us_2y", "us_10y",
    "us_real_10y", "us_breakeven_10y", "us_cpi", "us_core_cpi",
    "us_unemployment", "us_jobless", "us_hy_spread", "us_vix", "us_nfci",
    "us_dollar_index", "us_cfnai", "us_equal_weight_proxy",
    "us_semiconductor_proxy", "us_high_yield_proxy", "us_long_treasury_proxy",
    "gold", "wti", "copper",
}

PROXY_KEYS = {
    "us_equal_weight_proxy", "us_semiconductor_proxy",
    "us_high_yield_proxy", "us_long_treasury_proxy",
    "us_lqd_proxy", "us_regional_bank_proxy",
    "us_discretionary_proxy", "us_staples_proxy",
}


def _source_url(source: str, series: str) -> str:
    if source == "fred":
        return f"https://fred.stlouisfed.org/series/{series}"
    if source in {"ecos", "ecos_raw"}:
        return "https://ecos.bok.or.kr/"
    if source == "krx":
        return "https://openapi.krx.co.kr/"
    if source == "ecb":
        return f"https://data.ecb.europa.eu/data/datasets/{series.split('/')[0]}"
    if source == "boe":
        return (
            "https://www.bankofengland.co.uk/boeapps/database/"
            f"fromshowcolumns.asp?SeriesCodes={series}"
        )
    return f"https://finance.yahoo.com/quote/{series}"


@lru_cache(maxsize=None)
def _source_frequency(source: str, series: str) -> str:
    """Resolve frequency from provider metadata without a network request."""
    if source == "fred":
        try:
            return FRED_FREQUENCIES[series]
        except KeyError as exc:
            raise ValueError(f"FRED frequency is not declared: {series}") from exc
    if source == "yahoo":
        return "D"
    if source == "krx":
        return "D"
    if source == "ecos_raw":
        stat_code = series.split("/", 1)[0]
        try:
            return ECOS_RAW_FREQUENCIES[stat_code]
        except KeyError as exc:
            raise ValueError(f"ECOS raw frequency is not declared: {stat_code}") from exc
    if source == "ecb":
        return ECB_FREQUENCIES[series]
    if source == "boe":
        return BOE_FREQUENCIES[series]
    if source == "ecos":
        from pyecos import ECOS

        # A non-empty dummy key is sufficient for walking pyecos' bundled
        # curated tree. No provider call is made while reading IndicatorSpec.
        client = ECOS("catalog-metadata-only")
        try:
            node = client
            for part in series.split("."):
                node = getattr(node, part)
            cycle = node.spec.cycle
            return cycle.value if hasattr(cycle, "value") else str(cycle)
        finally:
            client.close()
    raise ValueError(f"unknown indicator source: {source}")


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------
# key -> {"label", "unit", "category", "source", "series", "future"}
def catalog() -> dict:
    """Indicator catalog. `future` holds known upcoming release dates."""
    c = {
        # ===== 한국 (ECOS) =====
        # 금리
        "kr_base_rate": ("한국 기준금리", "%", "금리", "ecos", "rate.base", ["2026-08-27", "2026-10-22", "2026-11-26"]),
        "kr_call": ("한국 콜금리", "%", "금리", "ecos", "rate.call", []),
        "kr_cd_91d": ("한국 CD 91일", "%", "금리", "ecos", "rate.cd_91d", []),
        "kr_koribor_3m": ("한국 KORIBOR 3M", "%", "금리", "ecos", "rate.koribor_3m", []),
        "kr_treasury_3y": ("한국 국고채 3년", "%", "금리", "ecos", "rate.treasury_3y", []),
        "kr_treasury_5y": ("한국 국고채 5년", "%", "금리", "ecos", "rate.treasury_5y", []),
        "kr_corp_bond_3y": ("한국 회사채 3년", "%", "금리", "ecos", "rate.corporate_bond_3y", []),
        "kr_msb_364d": ("한국 통안채 364일", "%", "금리", "ecos", "rate.msb_364d", []),
        # 아래는 pyecos 큐레이션에 없어 ECOS 원표(817Y002)에서 직접 가져옵니다.
        "kr_kofr": ("한국 KOFR (무위험지표금리)", "%", "금리", "ecos_raw", "817Y002/010901000", []),
        "kr_cp_91d": ("한국 CP 91일", "%", "금리", "ecos_raw", "817Y002/010503000", []),
        "kr_msb_91d": ("한국 통안증권 91일", "%", "금리", "ecos_raw", "817Y002/010400000", []),
        "kr_treasury_1y": ("한국 국고채 1년", "%", "금리", "ecos_raw", "817Y002/010190000", []),
        "kr_treasury_2y": ("한국 국고채 2년", "%", "금리", "ecos_raw", "817Y002/010195000", []),
        "kr_treasury_10y": ("한국 국고채 10년", "%", "금리", "ecos_raw", "817Y002/010210000", []),
        "kr_treasury_30y": ("한국 국고채 30년", "%", "금리", "ecos_raw", "817Y002/010230000", []),
        "kr_corp_bond_bbb": ("한국 회사채 3년 (BBB-)", "%", "금리", "ecos_raw", "817Y002/010320000", []),
        "kr_deposit_rate": ("한국 예금은행 수신금리", "%", "금리", "ecos", "rate.deposit", []),
        "kr_loan_rate": ("한국 예금은행 대출금리", "%", "금리", "ecos", "rate.loan", []),
        # 환율
        "kr_usd": ("원/달러", "원", "환율", "ecos", "fx.usd", []),
        "kr_jpy": ("원/엔", "원", "환율", "ecos", "fx.jpy", []),
        "kr_eur": ("원/유로", "원", "환율", "ecos", "fx.eur", []),
        "kr_cny": ("원/위안", "원", "환율", "ecos", "fx.cny", []),
        # 물가
        "kr_cpi": ("한국 CPI", "지수", "물가", "ecos", "price.cpi", []),
        "kr_core_cpi": ("한국 근원 CPI", "지수", "물가", "ecos", "price.core_cpi", []),
        "kr_living_cpi": ("한국 생활물가", "지수", "물가", "ecos", "price.living_cpi", []),
        "kr_ppi": ("한국 PPI", "지수", "물가", "ecos", "price.ppi", []),
        # 통화
        "kr_m1": ("한국 M1", "십억원", "통화", "ecos", "money.m1", []),
        "kr_m2": ("한국 M2", "십억원", "통화", "ecos", "money.m2", []),
        "kr_l": ("한국 L (유동성)", "십억원", "통화", "ecos", "money.l", []),
        "kr_lf": ("한국 LF (유동성)", "십억원", "통화", "ecos", "money.lf", []),
        # 신용
        "kr_total_loans": ("한국 총대출", "십억원", "신용", "ecos", "credit.total_loans", []),
        "kr_total_deposits": ("한국 총예금", "십억원", "신용", "ecos", "credit.total_deposits", []),
        "kr_household_credit": ("한국 가계신용", "십억원", "신용", "ecos", "credit.household_credit", []),
        "kr_household_delinq": ("한국 가계 연체율", "%", "신용", "ecos", "credit.household_delinquency_rate", []),
        # 주식·투자
        "kr_investor_deposits": ("한국 투자자예탁금", "원", "투자", "ecos", "stock.investor_deposits", []),
        # 주식시장 폭·밸류에이션 (월간 공식 통계)
        "kr_kospi_trading_value": ("코스피 거래대금", "천원", "시장", "ecos", "stock.kospi.trading_value", []),
        "kr_kospi_turnover": ("코스피 회전율", "%", "시장", "ecos", "stock.kospi.turnover", []),
        "kr_kospi_per": ("코스피 PER", "배", "시장", "ecos", "stock.kospi.per", []),
        "kr_kospi_dividend_yield": ("코스피 배당수익률", "%", "시장", "ecos", "stock.kospi.dividend_yield", []),
        "kr_kosdaq_trading_value": ("코스닥 거래대금", "천원", "시장", "ecos", "stock.kosdaq.trading_value", []),
        "kr_kosdaq_turnover": ("코스닥 회전율", "%", "시장", "ecos", "stock.kosdaq.turnover", []),
        # 대외
        "kr_current_account": ("한국 경상수지", "백만달러", "대외", "ecos", "external.current_account", []),
        "kr_external_debt": ("한국 대외채무", "백만달러", "대외", "ecos", "external.debt", []),
        # 심리
        "kr_consumer_sentiment": ("한국 소비자심리", "지수", "심리", "ecos", "sentiment.consumer", []),
        "kr_business_sentiment": ("한국 기업심리", "지수", "심리", "ecos", "sentiment.business", []),
        "kr_manufacturing_bsi": ("한국 제조업 BSI", "지수", "심리", "ecos", "sentiment.manufacturing_bsi", []),
        "kr_economic_sentiment": ("한국 경제심리지수", "지수", "심리", "ecos", "sentiment.economic", []),
        # 상품
        "kr_dubai_oil": ("두바이유", "달러", "상품", "ecos", "commodity.dubai_oil", []),
        "kr_gold": ("국내 금", "원", "상품", "ecos", "commodity.gold", []),
        # 부동산
        "kr_house_sales": ("한국 주택매매가", "지수", "부동산", "ecos", "real_estate.house_sales_price", []),
        "kr_house_jeonse": ("한국 전세가", "지수", "부동산", "ecos", "real_estate.house_jeonse_price", []),
        # 성장
        "kr_gdp_growth": ("한국 GDP 성장률", "%", "성장", "ecos", "growth.gdp_growth", []),
        "kr_private_consumption": ("한국 민간소비 증가율", "%", "성장", "ecos", "growth.private_consumption_growth", []),
        "kr_facilities_invest": ("한국 설비투자 증가율", "%", "성장", "ecos", "growth.facilities_investment_growth", []),
        "kr_exports_growth": ("한국 수출 증가율", "%", "성장", "ecos", "growth.exports_growth", []),
        # 월간 경기·생산·소비·투자
        "kr_leading_cycle": ("한국 선행지수순환변동치", "지수", "성장", "ecos", "business_cycle.leading_index", []),
        "kr_coincident_cycle": ("한국 동행지수순환변동치", "지수", "성장", "ecos", "business_cycle.coincident_index", []),
        "kr_manufacturing_output": ("한국 제조업생산지수", "지수", "성장", "ecos", "production.manufacturing.output", []),
        "kr_manufacturing_inventory": ("한국 제조업재고지수", "지수", "성장", "ecos", "production.manufacturing.inventory", []),
        "kr_manufacturing_utilization": ("한국 제조업가동률지수", "지수", "성장", "ecos", "production.manufacturing.utilization", []),
        "kr_retail_sales": ("한국 소매판매액지수", "지수", "성장", "ecos", "consumption.retail_sales", []),
        "kr_equipment_investment": ("한국 설비투자지수", "지수", "투자", "ecos", "investment.equipment", []),
        "kr_all_industry_output": ("한국 전산업생산지수", "2020=100", "성장", "ecos", "production.all_industry", []),
        "kr_services_output": ("한국 서비스업생산지수", "2020=100", "성장", "ecos", "production.services", []),
        "kr_manufacturing_shipments": ("한국 제조업출하지수", "2020=100", "성장", "ecos", "production.manufacturing.shipment", []),
        "kr_credit_card_spending": ("한국 개인신용카드사용액", "백만원", "성장", "ecos", "consumption.credit_card_spending", []),
        "kr_machinery_orders": ("한국 국내기계수주액", "백만원", "투자", "ecos", "investment.machinery_orders", []),
        "kr_building_permits": ("한국 건축허가면적", "㎡", "투자", "ecos", "investment.building_permits", []),
        "kr_construction_started": ("한국 건축착공면적", "㎡", "투자", "ecos", "investment.construction_started", []),
        "kr_employment_rate": ("한국 고용률", "%", "고용", "ecos", "employment.employment_rate", []),
        "kr_hourly_wage": ("한국 시간당명목임금지수", "2020=100", "고용", "ecos", "employment.hourly_wage", []),
        "kr_unit_labor_cost": ("한국 단위노동비용지수", "2020=100", "고용", "ecos", "employment.unit_labor_cost", []),
        # 대외건전성·수출·반도체 사이클
        "kr_fx_reserves": ("한국 외환보유액", "천달러", "대외", "ecos", "external.reserves.total", []),
        "kr_portfolio_liabilities": ("한국 증권투자(부채)", "백만달러", "대외", "ecos", "external.portfolio_investment_liabilities", []),
        "kr_export_value_index": ("한국 수출금액지수", "지수", "대외", "ecos", "trade.exports.value", []),
        "kr_export_volume_index": ("한국 수출물량지수", "지수", "대외", "ecos", "trade.exports.volume", []),
        "kr_semiconductor_export_value": ("한국 반도체 수출금액지수", "지수", "대외", "ecos", "trade.exports.semiconductor.value", []),
        "kr_semiconductor_export_volume": ("한국 반도체 수출물량지수", "지수", "대외", "ecos", "trade.exports.semiconductor.volume", []),
        "kr_semiconductor_export_price": ("한국 반도체 수출물가지수", "지수", "물가", "ecos", "trade.exports.semiconductor.price", []),
        "kr_terms_of_trade": ("한국 순상품교역조건지수", "지수", "대외", "ecos", "trade.terms_of_trade.net", []),
        "kr_dram_ppi": ("한국 DRAM 생산자물가", "지수", "물가", "ecos", "price.producer.dram", []),
        "kr_nand_ppi": ("한국 NAND 생산자물가", "지수", "물가", "ecos", "price.producer.nand", []),
        "kr_import_price": ("한국 수입물가지수", "2020=100", "물가", "ecos", "trade.imports.price", []),
        "kr_semiconductor_import_value": ("한국 반도체 수입금액지수", "2020=100", "대외", "ecos", "trade.imports.semiconductor.value", []),
        "kr_income_terms_of_trade": ("한국 소득교역조건지수", "2020=100", "대외", "ecos", "trade.terms_of_trade.income", []),
        "kr_logic_ppi": ("한국 시스템반도체 생산자물가", "2020=100", "물가", "ecos", "price.producer.logic", []),
        "kr_kospi_market_cap": ("코스피 시가총액", "천원", "시장", "ecos", "stock.kospi.market_cap", []),
        "kr_kosdaq_market_cap": ("코스닥 시가총액", "천원", "시장", "ecos", "stock.kosdaq.market_cap", []),
        # ===== 미국 (FRED) =====
        "us_cpi": ("미국 CPI", "지수", "물가", "fred", "CPIAUCSL", ["2026-09-11", "2026-10-14", "2026-12-10"]),
        "us_core_cpi": ("미국 근원 CPI", "지수", "물가", "fred", "CPILFESL", []),
        "us_pce": ("미국 PCE", "지수", "물가", "fred", "PCEPI", ["2026-08-26", "2026-09-30", "2026-10-29", "2026-11-25", "2026-12-23"]),
        "us_core_pce": ("미국 근원 PCE", "지수", "물가", "fred", "PCEPILFE", []),
        "us_ppi": ("미국 PPI", "지수", "물가", "fred", "PPIACO", ["2026-09-10", "2026-10-15", "2026-11-13", "2026-12-15"]),
        "us_rate": ("미국 실효연방기금금리", "%", "금리", "fred", "FEDFUNDS", ["2026-09-17", "2026-10-29", "2026-12-10"]),
        "us_sofr": ("미국 SOFR", "%", "금리", "fred", "SOFR", []),
        "us_3m": ("미국 3개월 국채", "%", "금리", "fred", "DGS3MO", []),
        "us_1y": ("미국 1년 국채", "%", "금리", "fred", "DGS1", []),
        "us_2y": ("미국 2년 국채", "%", "금리", "fred", "DGS2", []),
        "us_5y": ("미국 5년 국채", "%", "금리", "fred", "DGS5", []),
        "us_10y": ("미국 10년 국채", "%", "금리", "fred", "DGS10", []),
        "us_30y": ("미국 30년 국채", "%", "금리", "fred", "DGS30", []),
        "us_real_10y": ("미국 10년 실질금리", "%", "금리", "fred", "DFII10", []),
        "us_breakeven_10y": ("미국 10년 기대인플레이션", "%", "물가", "fred", "T10YIE", []),
        "us_term_premium_10y": ("미국 10년 기간 프리미엄", "%", "금리", "fred", "THREEFYTP10", []),
        "us_forward_inflation_5y5y": ("미국 5년 후 5년 기대인플레이션", "%", "물가", "fred", "T5YIFR", []),
        "us_unemployment": ("미국 실업률", "%", "고용", "fred", "UNRATE", []),
        "us_nfp": ("미국 비농업 고용", "천명", "고용", "fred", "PAYEMS", ["2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04"]),
        "us_jobless": ("미국 신규 실업수당", "천명", "고용", "fred", "ICSA", []),
        "us_continued_claims": ("미국 계속 실업수당", "명", "고용", "fred", "CCSA", []),
        "us_job_openings": ("미국 구인건수", "천건", "고용", "fred", "JTSJOL", ["2026-09-01", "2026-09-29", "2026-11-03", "2026-12-01"]),
        "us_quits_rate": ("미국 자발적 이직률", "%", "고용", "fred", "JTSQUR", []),
        "us_manufacturing_hours": ("미국 제조업 평균 주당근로시간", "시간", "성장", "fred", "AWHMAN", []),
        "us_gdp": ("미국 실질 GDP", "십억달러", "성장", "fred", "GDPC1", ["2026-08-26", "2026-09-30", "2026-10-29", "2026-11-25", "2026-12-23"]),
        "us_retail": ("미국 소매판매", "백만달러", "성장", "fred", "RRSFS", []),
        "us_housing": ("미국 주택착공", "천호", "부동산", "fred", "HOUST", []),
        "us_house_price": ("미국 주택가격", "지수", "부동산", "fred", "CSUSHPINSA", []),
        "us_durable": ("미국 내구재 주문", "백만달러", "성장", "fred", "DGORDER", []),
        "us_industrial_production": ("미국 산업생산지수", "지수", "성장", "fred", "INDPRO", []),
        "us_m2": ("미국 M2", "십억달러", "통화", "fred", "M2SL", []),
        "us_fed_assets": ("연준 총자산", "백만달러", "통화", "fred", "WALCL", []),
        "us_reserve_balances": ("연준 예치금 잔액", "백만달러", "통화", "fred", "WRESBAL", []),
        "us_tga": ("미 재무부 일반계정 (TGA)", "백만달러", "통화", "fred", "WTREGEN", []),
        "us_on_rrp": ("연준 익일물 역레포", "십억달러", "통화", "fred", "RRPONTSYD", []),
        "us_ig_spread": ("미국 IG 스프레드", "%", "신용", "fred", "BAMLC0A0CM", []),
        "us_bbb_spread": ("미국 BBB 스프레드", "%", "신용", "fred", "BAMLC0A4CBBB", []),
        "us_hy_spread": ("미국 HY 스프레드", "%", "신용", "fred", "BAMLH0A0HYM2", []),
        "us_vix": ("미국 VIX", "지수", "심리", "fred", "VIXCLS", []),
        "us_nfci": ("미국 금융여건지수", "지수", "신용", "fred", "NFCI", []),
        "us_financial_stress": ("미국 금융스트레스지수", "지수", "신용", "fred", "STLFSI4", []),
        "us_bank_lending_standards": ("미국 대형·중견기업 대출태도", "%p", "신용", "fred", "DRTSCILM", []),
        "us_consumer_sentiment": ("미국 소비자심리", "지수", "심리", "fred", "UMCSENT", []),
        "us_cfnai": ("미국 국가활동지수", "지수", "성장", "fred", "CFNAI", []),
        "us_sahm": ("미국 Sahm 침체지표", "%p", "성장", "fred", "SAHMREALTIME", []),
        "us_krw": ("원/달러 (FRED)", "원", "환율", "fred", "DEXKOUS", []),
        "us_dollar_index": ("미국 광의 달러지수", "지수", "환율", "fred", "DTWEXBGS", []),
        # ===== 유럽 (Eurozone) =====
        "eu_rate": ("ECB 예치금리", "%", "금리", "fred", "ECBDFR", []),
        "eu_hcpi": ("유로존 HICP", "지수", "물가", "fred", "CP0000EZ19M086NEST", []),
        "eu_gdp": ("유로존 GDP", "백만유로", "성장", "fred", "CLVMNACSCAB1GQEA19", []),
        "eur_usd": ("유로/달러", "달러", "환율", "fred", "DEXUSEU", []),
        # ===== 일본 =====
        # BOJ의 정책 목표는 무담보 익일물 콜금리이므로 3개월 인터뱅크 대용치보다
        # 개념적으로 정확하고, 공급자 갱신도 더 최신입니다.
        "jp_rate": ("일본 무담보 익일물 콜금리", "%", "금리", "fred", "IRSTCI01JPM156N", []),
        "jp_unemployment": ("일본 실업률", "%", "고용", "fred", "LRHUTTTTJPQ156S", []),
        "kr_unemployment": ("한국 실업률", "%", "고용", "ecos", "employment.unemployment_rate", []),
        "usd_jpy": ("엔/달러", "엔", "환율", "fred", "DEXJPUS", []),
        # ===== 중국 =====
        "cn_cpi": ("중국 CPI", "지수", "물가", "fred", "CHNCPIALLMINMEI", []),
        # OECD MEI가 2025-05 이후 FRED 갱신을 멈춰 남은 계열 중 가장 최신인
        # 지수 계열로 교체했습니다. 그래도 공급자 자체가 지연 상태입니다.
        "gb_cpi": ("영국 CPI (지수)", "지수", "물가", "fred", "GBRCPIALLMINMEI", []),
        "usd_cny": ("위안/달러", "위안", "환율", "fred", "DEXCHUS", []),
        # ===== 영국 =====
        # OECD 경유 FRED 계열은 공급자에서 수개월씩 갱신이 멈춰 각국 중앙은행
        # 원천으로 교체했습니다. 자세한 배경은 docs/lessons.md 참조.
        "gb_rate": ("영국 정책금리 (Bank Rate)", "%", "금리", "boe", "IUDBEDR", []),
        "gb_sonia": ("영국 SONIA 익일물", "%", "금리", "boe", "IUDSOIA", []),
        "eu_3m_rate": ("유로존 3개월 EURIBOR", "%", "금리", "ecb", "FM/M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA", []),
        # ===== 기타 신흥국 =====
        "usd_brl": ("브라질 헤알/달러", "헤알", "환율", "fred", "DEXBZUS", []),
        "usd_gbp": ("파운드/달러", "파운드", "환율", "fred", "DEXUSUK", []),
        # ===== KRX 파생 (krx_market 수집기가 채웁니다) =====
        # 지수옵션 풋/콜은 시장 포지셔닝이지 종목이 아니므로 계약 테이블이
        # 아니라 지표 계열로 둡니다.
        "kr_put_call_volume": ("코스피200 옵션 풋/콜 (거래량)", "배", "심리", "krx", "opt_bydd_trd/volume", []),
        "kr_put_call_value": ("코스피200 옵션 풋/콜 (거래대금)", "배", "심리", "krx", "opt_bydd_trd/value", []),
        "kr_put_call_open_interest": ("코스피200 옵션 풋/콜 (미결제)", "배", "심리", "krx", "opt_bydd_trd/open_interest", []),
        "kr_vkospi": ("코스피200 변동성지수 (VKOSPI)", "지수", "심리", "krx", "drvprod_dd_trd/코스피 200 변동성지수", []),
        # 거래소가 이미 보내주는 표에서 끌어올린 계열입니다. 새 공급자 승인이
        # 아니라 파생만으로 얇은 층을 채웁니다.
        "kr_breakeven_10y": ("한국 10년 기대인플레이션", "%p", "물가", "krx", "kts_bydd_trd/breakeven", []),
        "kr_treasury_20y": ("한국 국고채 20년", "%", "금리", "krx", "kts_bydd_trd/20y", []),
        "kr_bond_duration": ("한국 채권지수 평균 듀레이션", "년", "금리", "krx", "bon_dd_trd/duration", []),
        "kr_kospi200_futures_oi": ("코스피200 선물 미결제약정", "계약", "심리", "krx", "fut_bydd_trd/open_interest", []),
        "kr_kospi200_basis": ("코스피200 선물 베이시스", "%", "심리", "krx", "fut_bydd_trd/basis", []),
        "kr_etf_discount": ("ETF 할인 폭 (하위 5%)", "%p", "시장", "krx", "etf_bydd_trd/discount", []),
        "kr_gold_price": ("KRX 금 국내가", "원", "상품", "krx", "gold_bydd_trd/price", []),
        # ===== 상품 (Yahoo) =====
        "gold": ("국제 금 (선물)", "달러", "상품", "yahoo", "GC=F", []),
        "silver": ("국제 은 (선물)", "달러", "상품", "yahoo", "SI=F", []),
        "wti": ("WTI 유가", "달러", "상품", "yahoo", "CL=F", []),
        "brent": ("브렌트 유가", "달러", "상품", "yahoo", "BZ=F", []),
        "copper": ("국제 구리", "달러", "상품", "yahoo", "HG=F", []),
        # ===== 교차자산·시장폭 ETF (공식 지수 자체가 아닌 Yahoo proxy) =====
        "us_equal_weight_proxy": ("미국 동일가중 주식 (proxy)", "달러", "시장", "yahoo", "RSP", []),
        "us_semiconductor_proxy": ("미국 반도체 (proxy)", "달러", "시장", "yahoo", "SMH", []),
        "us_high_yield_proxy": ("미국 하이일드채 (proxy)", "달러", "시장", "yahoo", "HYG", []),
        "us_long_treasury_proxy": ("미국 장기국채 (proxy)", "달러", "시장", "yahoo", "TLT", []),
        "us_lqd_proxy": ("미국 투자등급 회사채 (proxy)", "달러", "신용", "yahoo", "LQD", []),
        "us_regional_bank_proxy": ("미국 지역은행 (proxy)", "달러", "시장", "yahoo", "KRE", []),
        "us_discretionary_proxy": ("미국 경기소비재 (proxy)", "달러", "시장", "yahoo", "XLY", []),
        "us_staples_proxy": ("미국 필수소비재 (proxy)", "달러", "시장", "yahoo", "XLP", []),
    }
    out = {}
    for key, (label, unit, cat, src, series, future) in c.items():
        out[key] = {
            "label": label, "unit": unit, "category": cat,
            "source": src, "series": series, "future": future,
            "frequency": _source_frequency(src, series),
            "source_options": (
                ECOS_SELECTORS.get(series, {}).copy() if src == "ecos" else {}
            ),
            "analysis_group": KEY_ANALYSIS_GROUP.get(
                key, CATEGORY_ANALYSIS_GROUP.get(cat, "supporting")
            ),
            "priority": "core" if key in CORE_KEYS else "supporting",
            "proxy": key in PROXY_KEYS,
            "source_url": _source_url(src, series),
        }
        out[key]["max_age_days"] = SOURCE_FRESHNESS_OVERRIDES.get(
            (src, series), DEFAULT_MAX_AGE_DAYS.get(out[key]["frequency"], 100)
        )
        out[key]["date_kind"] = date_kind_of(src, series, out[key]["frequency"])
    return out


# What a *rise* in this series means.  Not whether it is good — a rising
# policy rate is neither stress nor relief on its own — but whether the move
# tightens conditions or signals stress.  The interface needs it to avoid
# painting a widening credit spread like a rising index, and the
# normalization layer needs it to orient a percentile toward risk.
RISK = "up_is_risk"
NEUTRAL = "neutral"

# Deliberately partial.  A series nobody has classified is left out and gets
# no risk orientation at all, which is honest; a wrong default would be worse
# than none, because every reading built on it would be oriented backwards.
RISK_DIRECTION = {
    # 상승이 여건을 조이거나 스트레스를 뜻하는 계열
    "kr_usd": RISK,
    "us_dollar_index": RISK,
    "us_vix": RISK,
    "kr_vkospi": RISK,
    "us_ig_spread": RISK,
    "us_bbb_spread": RISK,
    "us_hy_spread": RISK,
    "us_nfci": RISK,
    "us_financial_stress": RISK,
    "us_bank_lending_standards": RISK,
    "kr_household_delinq": RISK,
    "kr_put_call_volume": RISK,
    # 할인 폭은 양수가 스트레스가 되도록 저장하므로 상승이 위험입니다.
    "kr_etf_discount": RISK,
    "kr_treasury_20y": NEUTRAL,
    "kr_bond_duration": NEUTRAL,
    "kr_gold_price": NEUTRAL,
    "kr_put_call_value": RISK,
    "kr_put_call_open_interest": RISK,
    # 상승이 그 자체로 조임도 완화도 아닌 계열
    "kr_base_rate": NEUTRAL,
    "kr_kofr": NEUTRAL,
    "kr_cd_91d": NEUTRAL,
    "kr_cp_91d": NEUTRAL,
    "kr_treasury_3y": NEUTRAL,
    "kr_treasury_10y": NEUTRAL,
    "kr_corp_bond_3y": NEUTRAL,
    "us_rate": NEUTRAL,
    "us_2y": NEUTRAL,
    "us_10y": NEUTRAL,
    "wti": NEUTRAL,
    "gold": NEUTRAL,
    "copper": NEUTRAL,
    "kr_cpi": NEUTRAL,
    "us_cpi": NEUTRAL,
    "kr_semiconductor_export_value": NEUTRAL,
}

# Levels that mean revert instead of drifting with a cycle.  These are judged
# against their whole record: a trailing year that happens to contain a crisis
# would let the crisis set the baseline and score itself as ordinary.  VKOSPI
# demonstrated exactly that — 56.29 read as the 41st percentile of its own
# turbulent year and the 14th of three years.
MEAN_REVERTING_LEVELS = {"kr_vkospi", "us_vix"}

# Levels that only travel one way, so distribution position is arithmetic
# rather than news.  Measured, not assumed: these sat above the 95th
# percentile at 18 of the last 20 observation points.
SATURATED_LEVELS = {"kr_call", "kr_cd_91d", "kr_msb_91d"}


def risk_direction(key: str) -> str | None:
    """What a rise means, or None when the series has not been classified."""
    return RISK_DIRECTION.get(key)


# Some series are produced by a collector that fetches a different thing —
# the put/call ratio falls out of the KRX option table, VKOSPI is one row of
# the derivative index table.  They belong in the catalog so agents can
# discover them and the coverage audit can see them, but they must not be
# fetched one key at a time like a provider series.
COLLECTOR_FED_SOURCES = {"krx"}


def is_collector_fed(key: str) -> bool:
    """True when another collector writes this series, not a direct fetch."""
    return catalog()[key]["source"] in COLLECTOR_FED_SOURCES


def date_kind_of(source: str, series: str, frequency: str) -> str:
    """Classify what a series' observation date means for coverage auditing.

    Everything below daily frequency is stamped with the start of the period
    it describes, including weekly series, which the providers publish on an
    exact seven-day stride.  That regularity is what lets a coverage audit
    name a missing observation without consulting a trading calendar.
    """
    if frequency != "D":
        return "period_start"
    if (source, series) in CALENDAR_DAY_SERIES:
        return "calendar_day"
    return "trading_day"


def categories() -> list[str]:
    """Ordered list of categories for the UI."""
    return ["금리", "환율", "물가", "통화", "신용", "고용", "성장", "투자", "심리", "시장", "상품", "부동산", "대외"]


# 카테고리 설명 (모든 지표에 공통 적용 — 사용성을 해치지 않는 선에서)
CATEGORY_DESC = {
    "금리": "중앙은행 기준금리와 시장금리. 금리 상승은 주식에 부정적, 하락은 긍정적.",
    "환율": "통화 간 교환 비율. 원화 약세는 수출주에 유리, 수입 물가는 상승.",
    "물가": "물가 상승률. 높으면 중앙은행이 금리를 올려 시장에 충격.",
    "통화": "시중 통화량. 늘면 물가·자산 가격 상승 압력.",
    "신용": "대출·신용 지표. 스프레드 확대는 위험 신호.",
    "고용": "고용 상태. 강하면 경제 성장, 물가 압력도 함께.",
    "성장": "경제 성장률. 강하면 기업 이익 증가.",
    "투자": "시장 참여 자금 규모.",
    "심리": "투자자·소비자 심리. 시장 분위기 파악.",
    "시장": "시장폭·밸류에이션·교차자산 proxy. 공식 지수와 proxy를 구분해 해석.",
    "상품": "원자재 가격 (금, 유가 등). 유가 상승은 물가 상승 압력.",
    "부동산": "주택 가격. 자산 가격 지표.",
    "대외": "국제 수지, 대외 채무.",
}

# 개별 지표 설명 (중요한 것만 추가)
# Written once, reviewed, and read at every depth the interface offers.
#
# Five layers, and the discipline that keeps them useful:
#   what    이 숫자가 무엇인지. 카탈로그와 어긋나면 안 됨 — 월평균인지
#           일별인지, 지수인지 비율인지까지 정확히.
#   why     왜 보는지. 이 계열이 알려주는 것.
#   how     읽는 법. 무엇이 높고 낮음이 역사적으로 무엇과 함께 나타났는지.
#           무엇을 하라고 말하지 않는다 — M7 게이트는 산문에도 적용된다.
#   watch   함께 볼 계열. 산문이 아니라 (카탈로그 키, 이유) 목록이다.
#           기계가 따라갈 수 있어야 M6.4의 연결 층이 그대로 물려받는다.
#           키가 실재하는지는 테스트가 강제한다.
#   caveat  이 계열 고유의 함정. proxy는 여기서 반드시 밝힌다.
#
# "지금 값의 뜻"은 여기 없다. 그것은 분포 판정에서 생성되며, 숫자가 든
# 문장을 여기 적으면 월요일이면 틀린 값이 된다.
EXPLANATIONS = {
    # ---- KRX 파생 (M6.3) ----
    "kr_breakeven_10y": {
        "what": "같은 달에 만기가 오는 국고채와 물가연동국채의 수익률 차이입니다. 채권시장이 앞으로 10년의 평균 물가를 얼마로 보는지를 가격에서 역산한 값입니다.",
        "why": "한국의 물가 계열은 전부 이미 일어난 물가를 알려줍니다. 이것만이 시장이 앞을 어떻게 보는지 말해 주며, 설문이 아니라 실제 돈이 걸린 기대치입니다.",
        "how": "이 값이 안정적이면 물가가 일시적으로 튀어도 기대가 고정돼 있다는 뜻입니다. 위로 풀리면 한국은행이 더 강하게 대응할 유인이 커집니다. 기준금리에서 이 값을 빼면 시장이 보는 실질 정책금리가 됩니다.",
        "watch": [
            ("kr_cpi", "실현 물가와 기대의 격차"),
            ("kr_base_rate", "기대 대비 정책 수준"),
            ("us_breakeven_10y", "글로벌 기대와 같이 움직이는지"),
        ],
        "caveat": "지표종목의 만기가 서로 어긋나는 기간에는 관측이 없습니다. 명목과 물가채 벤치마크가 같은 달 만기일 때만 계산하며, 비슷한 만기를 빼서 10년 기대인플레이션이라 부르지 않기 때문입니다. 지표종목이 새 종목으로 넘어갈 때 값이 튈 수 있는데, 이는 발행 사정이지 물가 소식이 아닙니다.",
    },
    "kr_treasury_20y": {
        "what": "잔존 20년 국고채 지표종목의 거래소 종가 수익률입니다. 한국은행 통계에는 없는 만기입니다.",
        "why": "10년과 30년 사이가 비어 있으면 곡선의 장기 구간 모양을 알 수 없습니다. 보험사·연기금의 수요가 집중되는 구간이라 수급 왜곡이 여기서 먼저 보입니다.",
        "how": "10년·30년과 함께 놓고 봅니다. 20년이 양쪽보다 낮으면 이 만기에 수요가 몰렸다는 뜻으로, 경기 전망이 아니라 수급의 결과일 수 있습니다.",
        "watch": [("kr_treasury_10y", "장기 구간 기울기"), ("kr_treasury_30y", "초장기 구간과의 관계")],
        "caveat": "거래소 종가 기준이라 한국은행이 발표하는 시장 평균 수익률과 소수점 단위로 다를 수 있습니다. 지표종목만 사용하므로 경과종목은 섞이지 않습니다.",
    },
    "kr_bond_duration": {
        "what": "KRX 채권지수의 평균 듀레이션입니다. 금리가 1%p 움직일 때 지수 가격이 대략 몇 % 움직이는지를 나타내는 연 단위 값입니다.",
        "why": "채권시장 전체가 금리 변화에 얼마나 민감한 상태인지 알려줍니다. 같은 금리 상승이라도 듀레이션이 길면 손실이 큽니다.",
        "how": "발행이 장기화되면 늘고 단기물 비중이 커지면 줄어듭니다. 듀레이션이 길어진 상태에서 금리가 오르면 채권 보유자의 손실이 커진다는 뜻으로 읽습니다.",
        "watch": [("kr_treasury_10y", "금리 방향"), ("kr_treasury_20y", "장기 발행 비중")],
        "caveat": "지수 구성의 성질이지 개별 포트폴리오의 위험이 아닙니다. 구성 종목이 바뀌면 시장 상황과 무관하게 움직입니다.",
    },
    "kr_kospi200_futures_oi": {
        "what": "코스피200 선물의 미결제약정 총합입니다. 주간 세션 전 월물을 더한 값이며 야간 세션은 제외합니다.",
        "why": "거래량이 하루의 활발함이라면 미결제약정은 실제로 남아 있는 포지션의 크기입니다. 시장에 걸린 판돈의 총량입니다.",
        "how": "가격 상승과 함께 늘면 새 자금이 들어온 것이고, 가격이 오르는데 줄면 기존 포지션이 정리되는 것입니다. 방향 자체는 이 값만으로 알 수 없습니다.",
        "watch": [("kr_kospi200_basis", "포지션의 방향 단서"), ("kr_put_call_open_interest", "옵션 쪽 포지셔닝")],
        "caveat": "만기일 전후로 월물이 넘어가면서 크게 변동합니다. 롤오버 구간의 변화는 포지션 변화가 아닙니다.",
    },
    "kr_kospi200_basis": {
        "what": "미결제약정이 가장 많은 근월물 선물 가격이 현물 지수보다 몇 % 높은지입니다. 이론적으로는 금리에서 배당수익률을 뺀 만큼 벌어집니다.",
        "why": "선물이 이론값보다 비싸거나 싼 정도는 차익거래 자금과 현물 수급의 상태를 보여줍니다. 프로그램 매매의 방향과 연결됩니다.",
        "how": "이론 수준보다 크게 낮으면(백워데이션) 현물 매도 압력이 강하다는 뜻으로 읽히는 경우가 많습니다. 절대 부호보다 평소 수준과의 차이를 봅니다.",
        "watch": [("kr_kospi200_futures_oi", "포지션 규모"), ("kr_base_rate", "이론 베이시스의 금리 성분")],
        "caveat": "만기가 가까워지면 베이시스는 기계적으로 0으로 수렴합니다. 월물이 넘어갈 때 생기는 톱니 모양은 시장 신호가 아니라 롤오버입니다. 롤오버 구간을 사이에 둔 수준 비교는 하지 마세요.",
    },
    "kr_etf_discount": {
        "what": "그날 거래된 ETF의 가격 대비 순자산가치 괴리를 모아 하위 5% 지점을 할인 폭으로 나타낸 값입니다. 양수가 클수록 할인이 깊습니다.",
        "why": "ETF가 순자산보다 싸게 거래되면 유동성 공급이 원활하지 않다는 뜻입니다. 시장 스트레스가 유동성 경로에 나타나는 첫 자리 중 하나입니다.",
        "how": "평상시에는 0 근처에서 얕게 움직입니다. 꼬리가 갑자기 깊어지면 특정 자산군의 호가가 벌어졌다는 신호로 읽습니다.",
        "watch": [("kr_vkospi", "변동성과 같이 움직이는지"), ("kr_kospi200_basis", "현물·파생 괴리도 함께 벌어졌는지")],
        "caveat": "상장·폐지로 구성이 계속 바뀌므로 아주 긴 기간의 수준 비교에는 주의가 필요합니다. 당일 거래가 없던 종목은 제외했는데, 멈춘 호가와 움직이는 순자산의 차이는 아무도 거래할 수 없던 괴리이기 때문입니다.",
    },
    "kr_gold_price": {
        "what": "KRX 금시장에서 거래된 순도 99.99% 1kg 금괴의 그램당 원화 가격입니다.",
        "why": "국제 금 가격을 원화로 환산한 값과 비교하면 국내 프리미엄이 나옵니다. 프리미엄은 국내 안전자산 수요와 조달 여건을 반영합니다.",
        "how": "국제 금과 환율을 함께 봐야 의미가 생깁니다. 국제 가격이 그대로인데 국내 가격만 오르면 국내 수요나 수급 문제입니다.",
        "watch": [("gold", "국제 금 가격"), ("kr_usd", "환산에 필요한 환율")],
        "caveat": "거래소 시장 가격이라 소매 금은방 시세와 다릅니다. 거래량이 적은 날에는 변동이 커집니다.",
    },
    # ---- commodities ----
    "copper": {
        "what": "국제 구리 선물 가격입니다. 달러 표시이며 근월물입니다.",
        "why": "건설·전력·제조 전반에 쓰여 실물 수요에 민감합니다. 경기 선행성이 있다고 여겨져 'Dr. Copper'로 불립니다.",
        "how": "상승은 글로벌 실물 수요 개선 신호로 읽히는 경우가 많습니다. 금과의 비율(구리/금)은 위험선호 대용으로도 쓰입니다.",
        "watch": [
            ("gold", "구리/금 비율"),
            ("us_cfnai", "실물 활동과 일치하는지"),
            ("kr_manufacturing_output", "국내 제조업"),
        ],
        "caveat": "중국 수요와 공급 차질에 크게 좌우되므로 글로벌 경기만의 함수가 아닙니다.",
    },
    "gold": {
        "what": "국제 금 선물 가격입니다. 달러 표시이며 근월물입니다.",
        "why": "이자를 낳지 않는 자산이라 실질금리가 낮을수록 상대적으로 유리합니다. 위험 회피와 통화가치 하락 우려를 동시에 반영합니다.",
        "how": "실질금리와 역방향으로 움직이는 경향이 있었습니다. 실질금리가 오르는데도 금이 오르면 통상적 관계 밖의 수요(지정학·중앙은행 매입)를 의심해 볼 여지가 있습니다.",
        "watch": [
            ("us_real_10y", "통상 역방향"),
            ("us_dollar_index", "달러 강세는 부담"),
        ],
        "caveat": "선물 근월물이며 국내 금시장 가격과는 환율·세제·수급 차이로 벌어집니다.",
    },
    "wti": {
        "what": "서부텍사스산 원유 선물 가격입니다. 미국 기준 유종이며 근월물 가격입니다.",
        "why": "물가의 가장 변동성 큰 구성요소이고, 에너지 수입국인 한국에는 교역조건에 직접 영향을 줍니다.",
        "how": "상승은 헤드라인 물가를 밀어올려 중앙은행의 부담을 키웁니다. 다만 수요 증가로 오르는 것과 공급 차질로 오르는 것은 경기 함의가 정반대입니다.",
        "watch": [
            ("copper", "수요發인지 판단 — 구리와 같이 오르면 수요"),
            ("us_cpi", "물가 전가"),
            ("kr_import_price", "국내 전가"),
        ],
        "caveat": "선물 근월물이라 만기 교체 시점에 가격이 점프할 수 있습니다. 현물 가격이 아닙니다.",
    },
    # ---- credit_stress ----
    "us_high_yield_proxy": {
        "what": "미국 하이일드 회사채에 투자하는 ETF(HYG)의 가격입니다. 스프레드 수치가 아니라 **거래되는 상품의 가격**입니다.",
        "why": "신용시장의 스트레스가 실제 자금 흐름과 손익으로 어떻게 나타나는지 매일 보여줍니다. 채권 지수보다 반응이 빠릅니다.",
        "how": "스프레드가 벌어지면 가격이 내립니다. 투자등급 ETF 대비 상대강도를 보면 위험선호의 방향이 드러납니다.",
        "watch": [
            ("us_hy_spread", "가격을 움직이는 스프레드"),
            ("us_lqd_proxy", "투자등급 대비 상대강도"),
        ],
        "caveat": "proxy입니다 — ETF 가격이며 공식 지수나 신용스프레드 자체가 아닙니다. 분배금과 유동성 프리미엄이 섞여 있어 스프레드 계열과 직접 비교하면 안 됩니다.",
    },
    "us_hy_spread": {
        "what": "미국 하이일드(투기등급) 회사채가 같은 만기 국채보다 얼마나 높은 금리를 주는지를 옵션조정 기준으로 계산한 값입니다.",
        "why": "신용위험의 가격입니다. 주식보다 먼저 스트레스를 반영하는 경우가 많아 위험선호의 조기 신호로 널리 쓰입니다.",
        "how": "좁으면 시장이 부도 위험을 낮게 본다는 뜻입니다. 빠르게 벌어지는 국면이 문제이고, 수준보다 확대 속도가 더 많은 정보를 담습니다.",
        "watch": [
            ("us_ig_spread", "우량 등급까지 번졌는지"),
            ("us_high_yield_proxy", "실제 가격으로 본 손익"),
            ("us_nfci", "금융여건 전반"),
        ],
        "caveat": "지수 편입 종목의 등급 구성이 시간에 따라 바뀌므로 아주 긴 기간의 수준 비교에는 주의가 필요합니다.",
    },
    "us_nfci": {
        "what": "시카고 연은 금융여건지수입니다. 자금·신용·레버리지 관련 105개 지표를 합성해 0이 평균이 되도록 표준화했습니다.",
        "why": "금리 하나가 아니라 금융시스템 전체가 조이는지 푸는지를 한 숫자로 봅니다.",
        "how": "양수는 평균보다 조인 상태, 음수는 완화된 상태입니다. 통화정책이 실제로 전달되고 있는지를 정책금리보다 잘 보여줍니다.",
        "watch": [
            ("us_hy_spread", "신용 쪽 기여"),
            ("us_sofr", "자금시장 쪽 기여"),
        ],
        "caveat": "주간이며 관측일은 해당 주의 시작일입니다. 과거 값이 함께 개정됩니다.",
    },
    # ---- fx_external ----
    "kr_fx_reserves": {
        "what": "한국은행이 보유한 외환보유액 총액입니다. 단위는 천달러입니다.",
        "why": "환율이 급변할 때 당국이 개입할 수 있는 여력이며, 대외 신인도의 기초 지표입니다.",
        "how": "월 단위로 완만하게 움직이므로 급격한 감소가 신호입니다. 다만 감소분에는 개입뿐 아니라 보유 자산의 평가손익과 환산 효과가 섞여 있습니다.",
        "watch": [
            ("kr_usd", "환율 압력의 크기"),
            ("kr_current_account", "외화의 구조적 유입"),
        ],
        "caveat": "월별이며 관측일은 해당 월 1일입니다. 개입 규모를 직접 알려주지는 않습니다.",
    },
    "kr_usd": {
        "what": "원/달러 환율입니다. 1달러를 사는 데 필요한 원화 금액이라, 값이 오르면 원화가 약해진 것입니다.",
        "why": "수출 기업의 채산성, 수입 물가, 외국인 자금의 유출입이 모두 여기에 걸립니다. 한국 자산의 대외 신인도를 가장 빠르게 반영합니다.",
        "how": "상승은 원화 약세로, 수입 물가를 올리고 외국인 투자 손익을 악화시킵니다. 달러지수와 함께 보면 원화 고유의 약세인지 달러 전반의 강세인지 구분됩니다.",
        "watch": [
            ("us_dollar_index", "달러 전반인지 원화 고유인지"),
            ("kr_fx_reserves", "당국의 대응 여력"),
            ("kr_import_price", "수입 물가 전가"),
        ],
        "caveat": "장중 고점·저점이 아니라 일별 기준환율입니다.",
    },
    "us_dollar_index": {
        "what": "연준이 산출하는 광의 달러지수입니다. 미국의 주요 교역상대국 통화 바스켓 대비 달러 가치를 지수화한 것으로, 널리 인용되는 DXY와는 구성이 다릅니다.",
        "why": "달러가 강해지면 신흥국 자금이 빠지고 원자재 가격이 눌립니다. 글로벌 위험선호를 가르는 축입니다.",
        "how": "상승은 달러 강세이며 통상 위험자산에 부담입니다. 원/달러가 오를 때 이 지수도 함께 오르면 원화 문제가 아니라 달러 문제입니다.",
        "watch": [
            ("kr_usd", "원화 고유 요인 분리"),
            ("copper", "달러 강세는 원자재에 부담"),
        ],
        "caveat": "FRED의 H.10 시리즈는 주 1회 공표라 정상 상태에서도 관측일이 8~10일 밀립니다. 결측이 아닙니다.",
    },
    # ---- growth_cycle ----
    "kr_leading_cycle": {
        "what": "통계청 경기선행지수의 순환변동치입니다. 추세를 제거해 경기 순환 성분만 남긴 값으로 100이 장기 추세선입니다.",
        "why": "실물 경기의 방향 전환을 앞서 알려주도록 설계된 지표입니다. 국내 경기 국면 판단의 출발점으로 널리 쓰입니다.",
        "how": "100을 넘는지보다 여러 달 연속 오르는지 내리는지가 중요합니다. 방향 전환이 신호이고 수준 자체는 해석이 제한적입니다.",
        "watch": [
            ("kr_manufacturing_output", "실제 생산이 따라오는지"),
            ("kr_semiconductor_export_value", "한국 경기의 실질 엔진"),
        ],
        "caveat": "월별이며 관측일은 해당 월 1일입니다. 사후 개정 폭이 큰 편이라 최근 1~2개월 값은 바뀔 수 있습니다.",
    },
    "kr_manufacturing_output": {
        "what": "제조업 생산지수입니다. 실제로 만들어진 물량을 지수화한 것으로 금액이 아닙니다.",
        "why": "한국 경제의 중심이 제조업이라, 실물 활동의 강도를 가장 직접적으로 보여줍니다.",
        "how": "전년동월비로 읽습니다. 선행지수가 먼저 돌고 생산이 뒤따르는지를 함께 보면 경기 전환의 확인이 됩니다.",
        "watch": [
            ("kr_leading_cycle", "선행지표와의 시차"),
            ("kr_semiconductor_export_value", "주도 업종"),
        ],
        "caveat": "월별이며 관측일은 해당 월 1일입니다. 조업일수와 명절 이동에 따라 월별 변동이 큽니다.",
    },
    "us_cfnai": {
        "what": "시카고 연은 국가활동지수입니다. 85개 월별 지표를 하나로 합성했고, 0이 과거 평균 성장 속도를 뜻하도록 표준화돼 있습니다.",
        "why": "미국 실물 경기의 전반 상태를 지표 하나로 요약합니다. 개별 지표의 잡음에 덜 흔들립니다.",
        "how": "0이 평균, 음수는 평균 이하 성장입니다. 3개월 이동평균이 −0.7 아래로 내려간 것이 과거 침체 국면과 자주 겹쳤습니다. 단일 지표로 침체를 확정하지는 못합니다.",
        "watch": [
            ("us_unemployment", "고용이 같이 꺾이는지"),
            ("us_nfci", "금융여건이 조이는지"),
        ],
        "caveat": "월별이며 관측일은 해당 월 1일입니다. 구성 지표가 개정되면 과거 값도 함께 바뀝니다.",
    },
    # ---- inflation ----
    "kr_cpi": {
        "what": "한국 소비자물가지수입니다. 가계가 사는 물건과 서비스의 가격을 묶어 지수로 만든 것으로, 수준(지수)이지 상승률이 아닙니다.",
        "why": "한국은행의 물가안정 목표가 이 지표를 대상으로 합니다. 정책금리 결정의 가장 큰 입력입니다.",
        "how": "지수 자체는 계속 오르므로 수준을 보는 의미가 적습니다. 12개월 전과 비교한 상승률로 읽어야 하고, 기준금리에서 이 상승률을 빼면 실질 정책금리가 됩니다.",
        "watch": [
            ("kr_core_cpi", "변동성 큰 품목을 뺀 기조적 흐름"),
            ("kr_base_rate", "실질 정책금리 계산"),
            ("kr_import_price", "수입 물가가 시차를 두고 전가됩니다"),
        ],
        "caveat": "월별이고 관측일은 해당 월 1일입니다. 발표는 다음 달 초에 이뤄지므로 최신 관측이 한 달 이상 과거인 것이 정상입니다.",
    },
    "us_breakeven_10y": {
        "what": "10년 명목국채 수익률에서 물가연동국채 수익률을 뺀 값입니다. 채권시장이 앞으로 10년의 평균 물가를 얼마로 보는지를 가격에서 역산한 것입니다.",
        "why": "설문이 아니라 실제 돈이 걸린 기대치입니다. 중앙은행이 물가 기대를 붙잡고 있는지 판단하는 시장 측 근거입니다.",
        "how": "이 값이 안정적이면 물가가 일시적으로 튀어도 기대가 고정돼 있다는 뜻입니다. 위로 풀리면 중앙은행이 더 강하게 대응할 유인이 생깁니다.",
        "watch": [
            ("us_10y", "명목금리"),
            ("us_real_10y", "실질금리"),
            ("us_forward_inflation_5y5y", "더 먼 미래의 기대"),
        ],
        "caveat": "유동성 프리미엄이 섞여 있어 순수한 기대치는 아닙니다. 스트레스 국면에는 TIPS 유동성 악화로 왜곡됩니다.",
    },
    "us_core_cpi": {
        "what": "미국 소비자물가에서 에너지와 식품을 제외한 근원 지수입니다.",
        "why": "유가와 농산물은 통화정책으로 통제되지 않고 변동이 큽니다. 이것들을 빼면 정책이 다룰 수 있는 기조적 물가가 남습니다.",
        "how": "헤드라인과 근원이 갈릴 때가 중요합니다. 헤드라인만 내리고 근원이 버티면 물가 둔화가 유가 덕분이라는 뜻이라 정책 전환 근거로는 약합니다.",
        "watch": [
            ("us_cpi", "헤드라인과의 격차"),
            ("us_core_pce", "연준의 목표 지표"),
        ],
        "caveat": "월별이며 관측일은 해당 월 1일입니다.",
    },
    "us_cpi": {
        "what": "미국 소비자물가지수입니다. 계절조정된 지수 수준이며 상승률이 아닙니다.",
        "why": "세계 금융시장이 가장 크게 반응하는 단일 지표입니다. 연준 정책 경로에 대한 기대를 발표 당일 바꿔 놓습니다.",
        "how": "전년동월비로 읽습니다. 다만 연준이 목표로 삼는 것은 CPI가 아니라 PCE이므로, 정책 판단에는 근원 PCE를 함께 봐야 합니다.",
        "watch": [
            ("us_core_cpi", "에너지·식품을 뺀 기조"),
            ("us_core_pce", "연준이 실제로 목표하는 지표"),
            ("us_breakeven_10y", "시장이 앞으로를 어떻게 보는지"),
        ],
        "caveat": "월별이며 관측일은 해당 월 1일입니다. 주거비 반영에 시차가 커서 실시간 임대료와 다르게 움직이는 구간이 있습니다.",
    },
    # ---- labor ----
    "us_jobless": {
        "what": "미국 신규 실업수당 청구건수입니다. 주간 지표이며 단위는 천 명입니다.",
        "why": "가장 빠른 고용 지표입니다. 월별 고용보고서보다 3주 이상 먼저 노동시장의 변화를 보여줍니다.",
        "how": "주별 변동이 커서 4주 이동평균으로 읽는 것이 관행입니다. 추세적 상승 전환이 고용 악화의 초기 신호로 자주 인용됐습니다.",
        "watch": [
            ("us_unemployment", "실업률로 확인되는지"),
            ("us_cfnai", "실물 전반"),
        ],
        "caveat": "주간이며 관측일은 해당 주 시작일입니다. 계절조정 방식 변경과 휴일 주간에 왜곡이 생깁니다.",
    },
    "us_unemployment": {
        "what": "미국 실업률입니다. 경제활동인구 중 일자리를 찾고 있는 사람의 비율입니다.",
        "why": "연준의 두 가지 책무 중 하나입니다. 물가와 함께 정책 결정의 축입니다.",
        "how": "낮은 것이 좋기만 한 것은 아닙니다. 과열은 임금發 물가 압력으로 이어집니다. 저점 대비 상승 폭이 일정 수준을 넘으면 침체와 겹친 사례가 있었습니다.",
        "watch": [
            ("us_jobless", "더 빠른 신호"),
            ("us_rate", "정책 대응"),
        ],
        "caveat": "월별이며 관측일은 해당 월 1일입니다. 구직을 포기하면 분모에서 빠지므로 실업률 하락이 항상 개선은 아닙니다.",
    },
    # ---- liquidity ----
    "us_sofr": {
        "what": "미국 국채를 담보로 하루 빌리는 실거래 기반 조달금리입니다. LIBOR를 대체한 달러 지표금리입니다.",
        "why": "달러 단기자금시장이 원활한지를 매일 보여줍니다. 담보가 있는 거래라 신용위험이 아니라 유동성 상태를 반영합니다.",
        "how": "연방기금금리 목표범위 안에서 움직이는 것이 정상입니다. 범위 상단을 튀어 오르면 담보 수요가 몰렸다는 뜻으로, 과거 자금시장 경색의 초기 신호였습니다.",
        "watch": [
            ("us_rate", "정책 목표 수준과의 관계"),
            ("us_nfci", "금융여건 전반이 같이 조였는지"),
        ],
        "caveat": "분기말·월말에 규제 대응 수요로 일시 급등하는 것은 구조적 현상이며 경색이 아닙니다.",
    },
    # ---- market_breadth ----
    "us_equal_weight_proxy": {
        "what": "S&P 500 구성 종목을 동일 비중으로 담는 ETF(RSP)의 가격입니다. 시가총액 가중이 아니라 **모든 종목을 같은 비중으로** 봅니다.",
        "why": "시총 가중 지수가 소수 대형주에 좌우될 때, 이것과 비교하면 상승이 넓은지 좁은지가 드러납니다.",
        "how": "S&P 500 대비 상대강도가 시장폭입니다. 지수는 오르는데 이것이 뒤처지면 소수 종목이 끌고 가는 좁은 장세입니다.",
        "watch": [
            ("us_semiconductor_proxy", "쏠림의 주역"),
            ("us_hy_spread", "위험선호가 실제로 넓은지"),
        ],
        "caveat": "proxy입니다 — ETF 가격이며 공식 지수가 아닙니다. 리밸런싱 비용과 운용보수가 반영돼 있습니다.",
    },
    "us_semiconductor_proxy": {
        "what": "미국 상장 반도체 기업에 투자하는 ETF(SMH)의 가격입니다.",
        "why": "반도체는 글로벌 IT 투자 사이클의 선행 지표이고, 한국 수출과 코스피의 주도 업종과 직접 연결됩니다.",
        "how": "S&P 500 대비 상대강도가 사이클 국면을 알려줍니다. 한국 반도체 수출 지표와 함께 보면 가격과 실물이 같은 방향인지 확인됩니다.",
        "watch": [
            ("kr_semiconductor_export_value", "한국 실물 수출"),
            ("kr_dram_ppi", "메모리 가격"),
            ("us_equal_weight_proxy", "쏠림 정도"),
        ],
        "caveat": "proxy입니다 — ETF 가격이며 공식 지수가 아닙니다. 소수 대형주 비중이 커서 개별 기업 이슈에 크게 흔들립니다.",
    },
    # ---- policy_rates ----
    "kr_base_rate": {
        "what": "한국은행 금융통화위원회가 결정하는 정책금리입니다. 시장에서 형성되는 금리가 아니라 정책적으로 정해지는 값이라, 바뀌지 않는 날에도 그날의 값이 존재합니다.",
        "why": "국내 모든 금리의 출발점입니다. 예금·대출·채권 금리가 여기에서 파생되고, 환율과 자산 가격도 이 수준과 변경 방향에 반응합니다.",
        "how": "수준 자체보다 방향과 속도가 중요합니다. 물가 상승률을 빼면 실질 정책금리가 되는데, 이것이 0 근처면 명목 인상에도 불구하고 실질적으로는 완화적일 수 있습니다.",
        "watch": [
            ("kr_cpi", "실질 정책금리를 계산하려면 물가가 필요합니다"),
            ("kr_kofr", "정책 의도가 실제 자금시장에 전달됐는지"),
            ("kr_treasury_3y", "시장이 앞으로의 정책 경로를 어떻게 보는지"),
        ],
        "caveat": "발표일이 아니라 적용일 기준으로 기록됩니다. 금통위 회의는 연 8회지만 계열은 매일 값을 갖습니다.",
    },
    "kr_call": {
        "what": "은행끼리 하루 빌려주는 무담보 초단기 금리입니다. 한국은행이 기준금리를 정하면 실제 시장에서 이 금리가 그 수준에 붙도록 유도합니다.",
        "why": "정책금리가 선언에 그치는지, 실제 자금시장까지 닿았는지를 보여주는 첫 확인점입니다.",
        "how": "기준금리와의 차이가 거의 없는 것이 정상입니다. 벌어지면 자금 수급에 긴장이 생겼다는 뜻이고, 분기말·연말처럼 자금 수요가 몰리는 시점에 자주 나타납니다.",
        "watch": [
            ("kr_base_rate", "정책금리와의 괴리가 전달 상태를 말해줍니다"),
            ("kr_kofr", "담보부 무위험금리와 비교하면 신용 프리미엄이 보입니다"),
        ],
        "caveat": "수준이 한 방향으로만 움직이는 구간이 길어 분포상 위치는 거의 항상 극단입니다. 위치보다 기준금리와의 차이를 보세요.",
    },
    "kr_cd_91d": {
        "what": "은행이 발행하는 91일 만기 양도성예금증서의 유통수익률입니다. 은행이 3개월 자금을 조달하는 비용이고, 다수 대출 상품의 기준이 됩니다.",
        "why": "은행 조달 여건을 직접 보여줍니다. 대출금리로 이어지므로 가계·기업의 이자 부담을 미리 알려줍니다.",
        "how": "무위험금리보다 얼마나 높은지가 은행 신용 프리미엄입니다. 정책금리 인상기에는 선반영되어 먼저 오르는 경향이 있습니다.",
        "watch": [
            ("kr_kofr", "무위험 대비 프리미엄"),
            ("kr_cp_91d", "은행 밖 조달과의 격차"),
        ],
        "caveat": "수준이 한 방향으로만 움직이는 구간이 길어 분포상 위치는 거의 항상 극단입니다. 변화폭과 스프레드를 보세요.",
    },
    "kr_corp_bond_3y": {
        "what": "신용등급 AA− 회사채 3년물의 유통수익률입니다. 우량 기업의 3년 조달비용입니다.",
        "why": "같은 만기 국고채와의 차이가 신용스프레드이고, 이것이 기업 자금조달 여건의 직접 지표입니다.",
        "how": "절대 수준보다 국고채 대비 격차가 중요합니다. 격차가 벌어지면 위험을 지는 대가가 커졌다는 뜻입니다. 격차가 좁아도 등급이 낮은 채권까지 좁은지는 별개 문제입니다.",
        "watch": [
            ("kr_treasury_3y", "차이가 신용스프레드"),
            ("kr_cp_91d", "단기 조달도 같이 긴장했는지"),
        ],
        "caveat": "AA− 우량 등급이라 저등급 시장의 스트레스는 잡아내지 못합니다. 등급별로 갈리는 국면이 실제로 존재합니다.",
    },
    "kr_cp_91d": {
        "what": "기업이 발행하는 91일 만기 기업어음의 유통수익률입니다. 은행을 거치지 않는 단기 자금조달 경로입니다.",
        "why": "은행 밖에서 기업이 돈을 빌리는 비용이라, 신용경색이 오면 CD보다 먼저 그리고 크게 반응합니다.",
        "how": "CD와의 차이가 단기 자금시장의 신용·유동성 긴장도입니다. 이 차이가 벌어지는 것은 조달 여건 악화 신호로 읽힙니다.",
        "watch": [
            ("kr_cd_91d", "CP−CD 차이가 긴장도입니다"),
            ("kr_corp_bond_3y", "만기가 긴 회사채도 같이 움직이는지"),
        ],
        "caveat": "발행 주체의 신용등급 구성이 시기마다 달라 수준 비교에는 주의가 필요합니다.",
    },
    "kr_kofr": {
        "what": "국채·통안증권을 담보로 하루 빌리는 실거래 기반 무위험지표금리입니다. 한국의 SOFR에 해당하며 CD·CP를 대체하는 지표금리로 육성되고 있습니다.",
        "why": "신용위험이 거의 없는 순수 조달비용이라, 다른 금리에서 이것을 빼면 그 금리가 담고 있는 신용·유동성 프리미엄이 드러납니다.",
        "how": "기준금리와 거의 같게 움직이는 것이 정상입니다. 아래로 벌어지면 단기자금이 남는다는 뜻이고, 위로 벌어지면 담보 수요가 몰렸다는 뜻입니다.",
        "watch": [
            ("kr_base_rate", "정책 전달 상태"),
            ("kr_cd_91d", "무위험 대비 은행 신용 프리미엄"),
        ],
        "caveat": "실거래 기반이라 거래가 적은 날에는 변동이 커질 수 있습니다.",
    },
    "kr_treasury_10y": {
        "what": "잔존 10년 국고채의 유통수익률입니다. 장기 자금의 기준 금리입니다.",
        "why": "장기 성장률과 물가에 대한 시장의 견해가 반영됩니다. 주택담보대출 같은 장기 금리와 주식 밸류에이션의 할인율에 영향을 줍니다.",
        "how": "3년물과의 차이(장단기 금리차)가 축소·역전되면 성장 기대 약화로 해석되는 경우가 많았습니다. 다만 그 자체로 침체를 확정하거나 시점을 예측하지는 못합니다.",
        "watch": [
            ("kr_treasury_3y", "장단기 금리차"),
            ("us_10y", "글로벌 장기금리와 동조하는지"),
        ],
        "caveat": "국민연금 등 장기 투자기관의 수급이 커서, 경기 전망과 무관한 이유로 움직이기도 합니다.",
    },
    "kr_treasury_3y": {
        "what": "잔존 3년 국고채의 유통수익률입니다. 국내 채권시장에서 가장 활발히 거래되는 지표 만기입니다.",
        "why": "시장이 앞으로 몇 년간의 정책금리 경로를 어떻게 보는지가 여기에 담깁니다. 기준금리가 그대로여도 이 금리는 먼저 움직입니다.",
        "how": "기준금리보다 높으면 시장이 추가 인상을 예상한다는 뜻이고, 낮으면 인하를 예상한다는 뜻으로 읽힙니다.",
        "watch": [
            ("kr_base_rate", "정책금리와의 차이가 시장 기대입니다"),
            ("kr_treasury_10y", "10년−3년이 장단기 금리차"),
            ("kr_corp_bond_3y", "같은 만기 회사채와의 차이가 신용스프레드"),
        ],
        "caveat": "국고채는 신용위험이 없으므로 이 금리의 변화는 정책 기대와 수급만 반영합니다.",
    },
    "us_10y": {
        "what": "미국 10년 국채 수익률입니다. 세계에서 가장 널리 참조되는 장기 무위험금리입니다.",
        "why": "전 세계 자산 가격의 할인율 기준입니다. 이 금리가 오르면 주식·부동산·신흥국 자산의 현재가치가 함께 눌립니다.",
        "how": "명목금리는 실질금리와 기대인플레이션의 합으로 분해해 읽습니다. 같은 상승이라도 실질금리가 올린 것인지 기대물가가 올린 것인지에 따라 의미가 다릅니다.",
        "watch": [
            ("us_real_10y", "상승의 원인이 실질금리인지"),
            ("us_breakeven_10y", "아니면 기대인플레이션인지"),
            ("us_2y", "장단기 금리차"),
        ],
        "caveat": "미국 국채는 글로벌 안전자산이라 미국 경제와 무관한 해외 수요로도 움직입니다.",
    },
    "us_2y": {
        "what": "미국 2년 국채 수익률입니다. 향후 2년의 정책금리 경로를 시장이 어떻게 보는지가 가장 직접적으로 담기는 만기입니다.",
        "why": "연준의 다음 행보에 대한 시장의 집단적 판단입니다. FOMC 발언이나 물가 지표에 즉각 반응합니다.",
        "how": "실효연방기금금리보다 낮으면 시장이 인하를 예상한다는 뜻입니다. 10년물과의 차이는 장단기 금리차로 널리 인용됩니다.",
        "watch": [
            ("us_10y", "10년−2년 장단기 금리차"),
            ("us_rate", "현재 정책 수준과의 격차"),
        ],
        "caveat": "정책 기대뿐 아니라 안전자산 수요에도 반응하므로, 위험 회피 국면에는 기대와 무관하게 하락할 수 있습니다.",
    },
    "us_long_treasury_proxy": {
        "what": "만기 20년 이상 미국 국채에 투자하는 ETF(TLT)의 가격입니다. 지수나 수익률이 아니라 **거래되는 상품의 가격**입니다.",
        "why": "장기금리 변화가 실제 자산 가격에 미치는 영향을 금리 숫자가 아니라 손익으로 보여줍니다. 듀레이션이 길어 금리 변화에 크게 반응합니다.",
        "how": "금리와 반대로 움직입니다. 금리가 오르면 가격이 내립니다. 위험 회피 국면에 주식과 반대로 가는지가 분산 효과의 확인점입니다.",
        "watch": [
            ("us_10y", "가격을 움직이는 금리"),
            ("us_real_10y", "실질금리 기여분"),
        ],
        "caveat": "proxy입니다 — ETF 가격이며 공식 지수나 국채 수익률 자체가 아닙니다. 분배금과 운용보수가 반영된 값이라 수익률 계열과 직접 비교하면 안 됩니다.",
    },
    "us_rate": {
        "what": "미국 실효연방기금금리입니다. FOMC가 발표하는 목표 범위 자체가 아니라, 그 범위 안에서 실제로 거래된 익일물 금리의 월평균입니다.",
        "why": "전 세계 달러 자금의 가격입니다. 여기가 움직이면 신흥국 자본 흐름과 환율이 따라 움직입니다.",
        "how": "월평균이라 회의 당월에는 인상 전후가 섞입니다. 목표 범위의 정확한 값이 필요하면 이 계열이 아니라 FOMC 발표를 보세요.",
        "watch": [
            ("us_2y", "시장의 정책 기대"),
            ("us_sofr", "일별 달러 자금시장 상태"),
        ],
        "caveat": "월별 계열이고 관측일은 해당 월의 1일로 기록됩니다. 발표 시점이 아니라 대상 기간의 시작일입니다.",
    },
    "us_real_10y": {
        "what": "미국 10년 물가연동국채(TIPS)의 수익률입니다. 물가 상승분을 보전해 주는 채권이므로 이 수익률이 실질 기준 할인율입니다.",
        "why": "명목금리 상승이 성장·정책 때문인지 물가 기대 때문인지 가르는 기준입니다. 실질금리 상승은 위험자산에 더 직접적인 압력입니다.",
        "how": "실질금리가 오르면 금과 장기 성장주처럼 현금흐름이 먼 자산이 상대적으로 불리해지는 경향이 있었습니다.",
        "watch": [
            ("us_10y", "명목금리"),
            ("us_breakeven_10y", "명목 − 실질 = 기대인플레이션"),
            ("gold", "실질금리와 역방향으로 움직이는 경향"),
        ],
        "caveat": "TIPS 시장은 명목국채보다 유동성이 낮아 스트레스 국면에 왜곡될 수 있습니다.",
    },
    # ---- sentiment ----
    "kr_consumer_sentiment": {
        "what": "한국은행 소비자동향조사의 소비자심리지수입니다. 100이 장기 평균이며 그보다 높으면 낙관이 우세하다는 뜻입니다.",
        "why": "소비는 GDP의 절반 안팎을 차지하고, 심리는 지출 결정에 선행합니다.",
        "how": "100 기준으로 읽되 수준보다 방향이 중요합니다. 물가가 오르면 심리가 먼저 꺾이는 관계가 자주 관찰됩니다.",
        "watch": [
            ("kr_cpi", "물가가 심리를 누르는지"),
            ("kr_leading_cycle", "실물 선행지표와 일치하는지"),
        ],
        "caveat": "월별이며 관측일은 해당 월 1일입니다. 설문 기반이라 실제 지출과 괴리가 생길 수 있습니다.",
    },
    "us_vix": {
        "what": "S&P 500 옵션 가격에서 역산한 향후 30일 기대 변동성입니다. 실제 변동성이 아니라 시장이 지불하는 보험료입니다.",
        "why": "위험 회피의 강도를 실시간으로 보여줍니다. 급등은 헤지 수요가 몰렸다는 뜻입니다.",
        "how": "수준이 낮다고 안전한 것이 아니라 시장이 안전하다고 믿는다는 뜻입니다. 장기간 낮은 상태가 이어진 뒤의 급등이 과거 조정과 자주 겹쳤습니다.",
        "watch": [
            ("kr_vkospi", "한국 시장의 같은 지표"),
            ("us_hy_spread", "신용시장도 같이 반응했는지"),
        ],
        "caveat": "평균회귀하는 계열이라 최근 1년이 아니라 전체 이력을 기준으로 위치를 봅니다.",
    },
    # ---- trade_semiconductors ----
    "kr_dram_ppi": {
        "what": "한국 DRAM 생산자물가지수입니다. 메모리 반도체의 출하 가격 수준입니다.",
        "why": "메모리 가격은 반도체 기업 이익에 직결되고, 사이클의 전환점을 물량보다 먼저 알려주는 경우가 많습니다.",
        "how": "전년동월비로 읽습니다. 수출 금액이 늘어도 이 가격이 꺾이고 있으면 단가가 아니라 물량이 끌고 있다는 뜻입니다.",
        "watch": [
            ("kr_semiconductor_export_value", "금액 중 단가 기여"),
            ("us_semiconductor_proxy", "주가가 선반영했는지"),
        ],
        "caveat": "월별이며 관측일은 해당 월 1일입니다. 계약 가격 기준이라 현물 가격 변화가 늦게 반영됩니다.",
    },
    "kr_semiconductor_export_value": {
        "what": "한국 반도체 수출금액지수입니다. 금액 기준이라 물량과 단가 변화가 함께 들어 있습니다.",
        "why": "반도체는 한국 수출의 최대 품목이고, 코스피 이익 사이클의 실질 엔진입니다.",
        "how": "전년동월비로 읽습니다. 금액이 오를 때 그것이 물량 때문인지 단가 때문인지는 가격 지수를 함께 봐야 갈립니다.",
        "watch": [
            ("kr_dram_ppi", "단가 기여분 분리"),
            ("us_semiconductor_proxy", "글로벌 주가와 일치하는지"),
            ("kr_manufacturing_output", "국내 생산"),
        ],
        "caveat": "월별이며 관측일은 해당 월 1일입니다. 지수이므로 절대 금액이 아닙니다.",
    },
}


INDICATOR_DESC = {
    "kr_base_rate": "한국 기준금리 — 금통위가 결정, 경제 전반에 영향.",
    "us_rate": "미국 실효연방기금금리 — FOMC 목표범위 자체가 아니라 실제 익일물 거래금리의 월평균.",
    "eu_rate": "ECB 예치금리 — 유로존 기준금리.",
    "us_cpi": "미국 CPI — 물가의 핵심 지표, 연준 정책에 영향.",
    "kr_usd": "원/달러 환율 — 수출입·외국인 투자에 영향.",
    "gold": "국제 금 가격 — 안전자산, 위험 회피 시 상승.",
    "wti": "WTI 유가 — 미국 기준 원유 가격.",
    "us_10y": "미국 10년 국채 — 장기 금리의 기준, 세계 자산 가격에 영향.",
    "us_ig_spread": "미국 투자등급 회사채 스프레드 — 신용 위험, 확대는 경계 신호.",
    "us_vix": "미국 VIX — 시장 공포 지수, 높으면 위험 회피.",
    "us_sofr": "미국 SOFR — 미 국채 담보 익일물 조달금리로 달러 단기자금시장 상태를 보여줍니다.",
    "us_real_10y": "미국 10년 실질금리 — 명목금리에서 기대물가 영향을 분리한 장기 할인율 참고치입니다.",
    "us_breakeven_10y": "미국 10년 기대인플레이션 — 명목 국채와 TIPS 수익률 차이로 계산된 시장 기대치입니다.",
    "us_nfci": "Chicago Fed NFCI — 0보다 높으면 장기 평균보다 긴축적, 낮으면 완화적인 금융여건입니다.",
    "us_financial_stress": "St. Louis Fed 금융스트레스지수 — 0보다 높으면 평균보다 스트레스가 큰 상태입니다.",
    "us_sahm": "Sahm 침체지표 — 0.50%p 이상은 경기침체 시작 신호지만 실시간 확정 판정은 아닙니다.",
    "kr_leading_cycle": "한국 선행지수순환변동치 — 향후 경기 방향의 월간 선행 참고치입니다.",
    "kr_fx_reserves": "한국 외환보유액 — 대외 유동성 완충력을 보여주는 월간 지표입니다.",
    "kr_semiconductor_export_value": "한국 반도체 수출금액지수 — 국내 대형 기술주 이익 사이클의 월간 실물 근거입니다.",
    "kr_dram_ppi": "DRAM 생산자물가 — 메모리 가격 사이클을 확인하는 월간 공식 통계입니다.",
    "us_equal_weight_proxy": "RSP ETF 종가 proxy — 시가총액가중 지수와 비교해 미국 주식 상승 폭을 점검합니다.",
    "us_semiconductor_proxy": "SMH ETF 종가 proxy — 미국 반도체 업종 흐름의 거래 가능한 대용치입니다.",
    "us_high_yield_proxy": "HYG ETF 종가 proxy — 하이일드채 가격 방향의 대용치이며 신용스프레드 자체는 아닙니다.",
    "us_long_treasury_proxy": "TLT ETF 종가 proxy — 미국 장기국채 가격 방향의 대용치이며 수익률 자체는 아닙니다.",
    "us_reserve_balances": "연준은행 준비금 잔액 — Fed 대차대조표 내 은행 유동성을 분해하는 주간 계열입니다.",
    "us_tga": "미 재무부 일반계정 — 잔액 증가는 다른 조건이 같다면 시중 달러 유동성을 흡수합니다.",
    "us_on_rrp": "연준 익일물 역레포 잔액 — Fed 총자산·TGA와 함께 달러 유동성 분해에 사용합니다.",
    "us_term_premium_10y": "10년 기간 프리미엄 — 장기금리에서 기대 단기금리 외의 기간 보상을 분리한 모형 추정치입니다.",
    "us_forward_inflation_5y5y": "5년 후 5년 기대인플레이션 — 단기 물가 충격에서 먼 장기 기대를 분리합니다.",
    "us_bank_lending_standards": "SLOOS 기업대출 태도 — 양수가 클수록 대출 기준을 강화한 은행의 순비율이 높습니다.",
    "us_lqd_proxy": "LQD ETF 종가 proxy — 투자등급 회사채 가격 방향의 대용치이며 IG 스프레드 자체가 아닙니다.",
    "us_regional_bank_proxy": "KRE ETF 종가 proxy — 미국 지역은행의 신용·경기 민감도를 교차 확인합니다.",
    "us_discretionary_proxy": "XLY ETF 종가 proxy — 경기소비재 가격 방향을 보여줍니다.",
    "us_staples_proxy": "XLP ETF 종가 proxy — 필수소비재 가격 방향을 보여줍니다.",
}


def category_description(category: str) -> str:
    return CATEGORY_DESC.get(category, "")


def indicator_description(key: str) -> str:
    """개별 지표 설명 (없으면 카테고리 설명으로 대체)."""
    if key in INDICATOR_DESC:
        return INDICATOR_DESC[key]
    spec = catalog().get(key, {})
    return CATEGORY_DESC.get(spec.get("category", ""), "")


def fetch_indicator(key: str) -> dict:
    """Fetch one indicator's series + metadata."""
    spec = catalog()[key]
    if spec["source"] == "fred":
        series = fred_series(spec["series"])
    elif spec["source"] == "ecos_raw":
        series = ecos_raw_series(spec["series"])
    elif spec["source"] == "ecb":
        series = ecb_series(spec["series"])
    elif spec["source"] == "boe":
        series = boe_series(spec["series"])
    elif spec["source"] == "yahoo":
        from app.collectors import quotes as qmod

        series = qmod.history(spec["series"], "2y")  # already exchange-dated
    else:
        series = ecos_series(spec["series"], selectors=spec["source_options"])
    return {"key": key, **spec, "series": series}


def fetch_all_into_db() -> dict:
    """Fetch all indicators and store them in the DB. Returns per-key result counts.

    One failing source won't stop the others.
    """
    return fetch_keys_into_db(list(catalog().keys()))


def fetch_keys_into_db(keys: list[str]) -> dict:
    """Fetch a subset of indicators into the DB."""
    from app import db

    db.init_db()
    results: dict = {}
    for key in keys:
        try:
            data = fetch_indicator(key)
            if not data["series"]:
                raise ValueError("source returned no observations")
            db.save_indicator_points(key, data["series"], source=data["source"])
            results[key] = len(data["series"])
        except Exception as ex:  # noqa: BLE001
            results[key] = f"ERR: {ex}"
    return results


def cycle_of(key: str) -> str:
    """Return the catalog's provider-declared data cycle."""
    return catalog()[key]["frequency"]


def by_cycle(cycle: str) -> list[str]:
    """Keys of indicators matching the given cycle."""
    return [k for k in catalog() if cycle_of(k) == cycle]


def freshness_days(key: str) -> int:
    """Return the provider-aware freshness allowance for one catalog key."""
    return int(catalog()[key]["max_age_days"])


def coverage_summary() -> dict:
    """Summarize whether cached evidence is ready for broad market analysis."""
    from app import db

    stored = db.get_indicator_overview()
    today = kst_today()
    counts = {"total": 0, "fresh": 0, "stale": 0, "missing": 0, "invalid": 0}
    core = {"total": 0, "fresh": 0, "stale": 0, "missing": 0, "invalid": 0}
    by_source: dict[str, dict[str, int]] = {}
    by_group: dict[str, dict[str, int]] = {}
    for key, spec in catalog().items():
        row = stored.get(key) or {}
        latest = row.get("max_date")
        if not latest:
            status = "missing"
        else:
            try:
                age = (today - date.fromisoformat(latest)).days
                status = "fresh" if age <= spec["max_age_days"] else "stale"
            except (TypeError, ValueError):
                status = "invalid"
        counts["total"] += 1
        counts[status] += 1
        if spec["priority"] == "core":
            core["total"] += 1
            core[status] += 1
        for bucket, name in (
            (by_source, spec["source"]),
            (by_group, spec["analysis_group"]),
        ):
            item = bucket.setdefault(
                name, {"total": 0, "fresh": 0, "stale": 0, "missing": 0, "invalid": 0}
            )
            item["total"] += 1
            item[status] += 1
    return {
        "series": counts,
        "core": core,
        "core_ready_pct": round(core["fresh"] / core["total"] * 100, 1)
        if core["total"] else 0.0,
        "by_source": by_source,
        "by_analysis_group": by_group,
    }
