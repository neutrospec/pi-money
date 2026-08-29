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
