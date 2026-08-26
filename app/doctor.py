"""Setup check: `uv run python -m app.doctor`.

Answers the one question a newcomer actually has — "are my keys working, and
what do I still have to do?" — by calling each provider once and reporting
what came back.  Nothing here writes to the database, so it is safe to run
before the first collection and any time a key or approval changes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

OK, WARN, FAIL = "✅", "⚠️ ", "❌"


def _probe_ecos(key: str) -> tuple[str, str]:
    """ECOS rejects an unknown key with a body, not an HTTP status."""
    response = httpx.get(
        f"https://ecos.bok.or.kr/api/StatisticItemList/{key}/json/kr/1/1/817Y002",
        timeout=20,
    )
    payload = response.json()
    if "RESULT" in payload:
        return FAIL, payload["RESULT"].get("MESSAGE", "인증 실패")
    if "StatisticItemList" in payload:
        return OK, "정상 (시장금리 표 조회 성공)"
    return FAIL, f"예상 밖 응답: {str(payload)[:80]}"


def _probe_fred(key: str) -> tuple[str, str]:
    response = httpx.get(
        "https://api.stlouisfed.org/fred/series",
        params={"series_id": "DGS10", "api_key": key, "file_type": "json"},
        timeout=20,
    )
    if response.status_code == 200:
        return OK, "정상 (DGS10 조회 성공)"
    if response.status_code in (400, 403):
        return FAIL, "키가 거부됐습니다. 32자 소문자 키인지 확인하세요."
    return FAIL, f"HTTP {response.status_code}"


def _probe_public(label: str, url: str, params: dict) -> tuple[str, str]:
    try:
        response = httpx.get(
            url, params=params,
            headers={"User-Agent": "money-market-intelligence/0.2"},
            timeout=25, follow_redirects=True,
        )
        return (
            (OK, "정상 (인증 불필요)") if response.status_code == 200
            else (WARN, f"HTTP {response.status_code} — 일시 장애일 수 있습니다")
        )
    except Exception as exc:
        return WARN, f"{type(exc).__name__}: {str(exc)[:60]}"


def _probe_krx(key: str) -> tuple[list[tuple[str, str, bool]], str]:
    """Return per-dataset approval so the operator knows what to apply for."""
    from datetime import timedelta

    from app.collectors import krx
    from app.timeutil import kst_today

    day = kst_today()
    while day.weekday() >= 5:            # step back to a weekday
        day -= timedelta(days=1)
    day -= timedelta(days=1)
    stamp = day.strftime("%Y%m%d")
    results = []
    for spec in krx.dataset_specs():
        try:
            response = httpx.get(
                f"https://data-dbg.krx.co.kr/svc/apis/{spec['path']}",
                params={"basDd": stamp},
                headers={"AUTH_KEY": key},
                timeout=25,
            )
            results.append((spec["dataset"], spec["label"], response.status_code == 200))
        except Exception:
            results.append((spec["dataset"], spec["label"], False))
    return results, stamp


# Datasets an analysis actually consumes today.  Anything outside this set can
# be approved later without losing a feature, so the report says so rather
# than sending someone through thirty application forms.
KRX_REQUIRED = {
    "stk_bydd_trd": "KOSPI 시장폭 (등락종목수·거래대금·쏠림)",
    "ksq_bydd_trd": "KOSDAQ 시장폭",
    "opt_bydd_trd": "시장 심리 게이지의 풋/콜 비율",
}
KRX_USEFUL = {
    "etf_bydd_trd": "ETF 공식 종가 (Yahoo 대체)",
    "kospi_dd_trd": "KOSPI 시리즈 지수",
    "kosdaq_dd_trd": "KOSDAQ 시리즈 지수",
}


def main() -> int:
    print("Money Market Intelligence — 설정 점검\n")
    if not ENV_PATH.exists():
        print(f"{FAIL} .env 파일이 없습니다.")
        print("   cp .env.example .env  로 만든 뒤 키를 채우세요.")
        print("   자세한 절차: docs/sources/setup.md\n")
        return 1
    load_dotenv(ENV_PATH, override=True)
    print(f"{OK} .env 발견: {ENV_PATH}\n")

    problems = 0
    print("── 필수 ────────────────────────────────")
    for label, variable, probe in (
        ("한국은행 ECOS", "ECOS_API_KEY", _probe_ecos),
        ("FRED", "FRED_API_KEY", _probe_fred),
    ):
        key = os.environ.get(variable, "").strip()
        if not key:
            print(f"{FAIL} {label:14s} {variable} 미설정 — 수집 대부분이 실패합니다")
            problems += 1
            continue
        try:
            mark, detail = probe(key)
        except Exception as exc:
            mark, detail = FAIL, f"{type(exc).__name__}: {str(exc)[:60]}"
        print(f"{mark} {label:14s} {detail}")
        if mark == FAIL:
            problems += 1

    print("\n── 인증 불필요 ─────────────────────────")
    for label, url, params in (
        ("Yahoo Finance", "https://query1.finance.yahoo.com/v8/finance/chart/^KS11",
         {"range": "5d", "interval": "1d"}),
        ("ECB Data Portal",
         "https://data-api.ecb.europa.eu/service/data/FM/M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA",
         {"format": "jsondata", "lastNObservations": "1"}),
        ("Bank of England",
         "https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp",
         {"csv.x": "yes", "Datefrom": "01/Jan/2026", "Dateto": "now",
          "SeriesCodes": "IUDBEDR", "CSVF": "TN", "UsingCodes": "Y",
          "VPD": "Y", "VFD": "N"}),
    ):
        mark, detail = _probe_public(label, url, params)
        print(f"{mark} {label:16s} {detail}")

    print("\n── 선택: KRX (데이터셋별 승인 필요) ────")
    krx_key = os.environ.get("KRX_API_KEY", "").strip()
    if not krx_key:
        print(f"{WARN} KRX_API_KEY 미설정 — 국내 시장폭만 비활성화됩니다.")
        print("   나머지 기능은 모두 정상 동작합니다.")
    else:
        results, stamp = _probe_krx(krx_key)
        approved = {name for name, _, ok in results if ok}
        print(f"   기준일 {stamp} · 승인 {len(approved)}/{len(results)}\n")
        missing_required = [
            (name, why) for name, why in KRX_REQUIRED.items() if name not in approved
        ]
        missing_useful = [
            (name, why) for name, why in KRX_USEFUL.items() if name not in approved
        ]
        for name, why in KRX_REQUIRED.items():
            mark = OK if name in approved else FAIL
            print(f"{mark} {name:16s} {why}")
        for name, why in KRX_USEFUL.items():
            mark = OK if name in approved else WARN
            print(f"{mark} {name:16s} {why}")
        others = [
            name for name, _, ok in results
            if name not in KRX_REQUIRED and name not in KRX_USEFUL and not ok
        ]
        if others:
            print(f"   그 외 미승인 {len(others)}개는 소비하는 분석이 없어 "
                  "신청하지 않아도 됩니다.")
        if missing_required:
            problems += 1
            print(f"\n{FAIL} 신청이 필요한 항목:")
            for name, why in missing_required + missing_useful:
                print(f"   - {name} → {why}")
            print("   신청처: https://openapi.krx.co.kr/ (서비스별 이용 신청)")
            print("   승인 후: uv run python -m app.collect "
                  "--history-reset --history-kind krx_access")

    print("\n────────────────────────────────────────")
    if problems:
        print(f"{FAIL} 해결할 항목 {problems}개가 있습니다. "
              "docs/sources/setup.md 를 참고하세요.")
        return 1
    print(f"{OK} 설정 완료. 다음 명령으로 수집을 시작하세요:")
    print("   uv run uvicorn app.main:app --host 127.0.0.1 --port 8077")
    return 0


if __name__ == "__main__":
    sys.exit(main())
