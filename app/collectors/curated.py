"""Official macro-event baseline normalized to Asia/Seoul.

Every timed event retains its source-local fields and is converted with the
standard zoneinfo library, so daylight-saving transitions are handled.
Events whose publication time is not officially fixed remain time-unknown.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")

FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS_URL = "https://www.bls.gov/schedule/2026/home.htm"
BEA_URL = "https://www.bea.gov/news/schedule"
BOK_URL = "https://www.bok.or.kr/portal/bbs/B0000502/view.do?menuNo=200690&nttId=10094300"
ECB_URL = "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"
BOJ_URL = "https://www.boj.or.jp/en/mopo/mpmsche_minu/m_ref/index.htm"
BOE_URL = "https://www.bankofengland.co.uk/news/2025/september/monetary-policy-committee-dates-for-2026"


@dataclass(frozen=True)
class EventSpec:
    source_date: str
    source_time: str | None
    source_timezone: str
    country: str
    title: str
    impact: str
    note: str
    source_url: str


# Dates are publication/decision dates in the source institution's timezone.
# Timed entries use official standard release times. BOJ/BOK decisions can be
# published at variable times and deliberately remain time-unknown.
CURATED = [
    EventSpec("2026-09-16", "14:00", "America/New_York", "US", "FOMC 금리 결정", "high", "성명 14:00 ET · 기자회견 14:30 ET", FED_URL),
    EventSpec("2026-10-28", "14:00", "America/New_York", "US", "FOMC 금리 결정", "high", "성명 14:00 ET · 기자회견 14:30 ET", FED_URL),
    EventSpec("2026-12-09", "14:00", "America/New_York", "US", "FOMC 금리 결정", "high", "성명 14:00 ET · 기자회견 14:30 ET", FED_URL),

    EventSpec("2026-08-27", None, "Asia/Seoul", "KR", "한국은행 금통위 기준금리", "high", "발표 시각은 한국은행 당일 공지 확인", BOK_URL),
    EventSpec("2026-10-22", None, "Asia/Seoul", "KR", "한국은행 금통위 기준금리", "high", "발표 시각은 한국은행 당일 공지 확인", BOK_URL),
    EventSpec("2026-11-26", None, "Asia/Seoul", "KR", "한국은행 금통위 기준금리", "high", "발표 시각은 한국은행 당일 공지 확인", BOK_URL),

    EventSpec("2026-09-04", "08:30", "America/New_York", "US", "미국 고용보고서 (NFP)", "high", "8월 고용 · 08:30 ET", BLS_URL),
    EventSpec("2026-09-11", "08:30", "America/New_York", "US", "미국 CPI", "high", "8월 물가 · 08:30 ET", BLS_URL),
    EventSpec("2026-10-02", "08:30", "America/New_York", "US", "미국 고용보고서 (NFP)", "high", "9월 고용 · 08:30 ET", BLS_URL),
    EventSpec("2026-10-14", "08:30", "America/New_York", "US", "미국 CPI", "high", "9월 물가 · 08:30 ET", BLS_URL),
    EventSpec("2026-11-06", "08:30", "America/New_York", "US", "미국 고용보고서 (NFP)", "high", "10월 고용 · 08:30 ET", BLS_URL),
    EventSpec("2026-12-04", "08:30", "America/New_York", "US", "미국 고용보고서 (NFP)", "high", "11월 고용 · 08:30 ET", BLS_URL),
    EventSpec("2026-12-10", "08:30", "America/New_York", "US", "미국 CPI", "high", "11월 물가 · 08:30 ET", BLS_URL),

    EventSpec("2026-09-01", "10:00", "America/New_York", "US", "미국 JOLTS", "medium", "7월 구인·이직 · 10:00 ET", BLS_URL),
    EventSpec("2026-09-29", "10:00", "America/New_York", "US", "미국 JOLTS", "medium", "8월 구인·이직 · 10:00 ET", BLS_URL),
    EventSpec("2026-11-03", "10:00", "America/New_York", "US", "미국 JOLTS", "medium", "9월 구인·이직 · 10:00 ET", BLS_URL),
    EventSpec("2026-12-01", "10:00", "America/New_York", "US", "미국 JOLTS", "medium", "10월 구인·이직 · 10:00 ET", BLS_URL),
    EventSpec("2026-09-10", "08:30", "America/New_York", "US", "미국 PPI", "medium", "8월 생산자물가 · 08:30 ET", BLS_URL),
    EventSpec("2026-10-15", "08:30", "America/New_York", "US", "미국 PPI", "medium", "9월 생산자물가 · 08:30 ET", BLS_URL),
    EventSpec("2026-11-13", "08:30", "America/New_York", "US", "미국 PPI", "medium", "10월 생산자물가 · 08:30 ET", BLS_URL),
    EventSpec("2026-12-15", "08:30", "America/New_York", "US", "미국 PPI", "medium", "11월 생산자물가 · 08:30 ET", BLS_URL),

    EventSpec("2026-08-26", "08:30", "America/New_York", "US", "미국 GDP", "high", "2분기 2차 추정치 · 08:30 ET", BEA_URL),
    EventSpec("2026-08-26", "08:30", "America/New_York", "US", "미국 PCE", "high", "7월 개인소비지출 · 08:30 ET", BEA_URL),
    EventSpec("2026-09-30", "08:30", "America/New_York", "US", "미국 PCE", "high", "8월 개인소비지출 · 08:30 ET", BEA_URL),
    EventSpec("2026-10-29", "08:30", "America/New_York", "US", "미국 GDP", "high", "3분기 속보치 · 08:30 ET", BEA_URL),
    EventSpec("2026-10-29", "08:30", "America/New_York", "US", "미국 PCE", "high", "9월 개인소비지출 · 08:30 ET", BEA_URL),
    EventSpec("2026-11-25", "08:30", "America/New_York", "US", "미국 GDP", "high", "3분기 수정치 · 08:30 ET", BEA_URL),
    EventSpec("2026-11-25", "08:30", "America/New_York", "US", "미국 PCE", "high", "10월 개인소비지출 · 08:30 ET", BEA_URL),
    EventSpec("2026-12-23", "08:30", "America/New_York", "US", "미국 GDP", "high", "3분기 확정치 · 08:30 ET", BEA_URL),
    EventSpec("2026-12-23", "08:30", "America/New_York", "US", "미국 PCE", "high", "11월 개인소비지출 · 08:30 ET", BEA_URL),

    EventSpec("2026-09-10", "14:15", "Europe/Paris", "EU", "ECB 금리 결정", "high", "결정 14:15 현지 · 기자회견 후속", ECB_URL),
    EventSpec("2026-10-29", "14:15", "Europe/Paris", "EU", "ECB 금리 결정", "high", "결정 14:15 현지 · 기자회견 후속", ECB_URL),
    EventSpec("2026-12-17", "14:15", "Europe/Paris", "EU", "ECB 금리 결정", "high", "결정 14:15 현지 · 기자회견 후속", ECB_URL),

    EventSpec("2026-09-18", None, "Asia/Tokyo", "JP", "BOJ 금리 결정", "high", "발표 시각 미정 · 총재 기자회견 15:30 JST", BOJ_URL),
    EventSpec("2026-10-30", None, "Asia/Tokyo", "JP", "BOJ 금리 결정", "high", "발표 시각 미정", BOJ_URL),
    EventSpec("2026-12-18", None, "Asia/Tokyo", "JP", "BOJ 금리 결정", "high", "발표 시각 미정", BOJ_URL),

    EventSpec("2026-09-17", "12:00", "Europe/London", "GB", "BOE 금리 결정", "high", "12:00 런던", BOE_URL),
    EventSpec("2026-11-05", "12:00", "Europe/London", "GB", "BOE 금리 결정", "high", "12:00 런던", BOE_URL),
    EventSpec("2026-12-17", "12:00", "Europe/London", "GB", "BOE 금리 결정", "high", "12:00 런던", BOE_URL),
]


def to_kst(source_date: str, source_time: str, source_timezone: str) -> datetime:
    local = datetime.strptime(
        f"{source_date} {source_time}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=ZoneInfo(source_timezone))
    return local.astimezone(KST)


def load() -> list[dict]:
    out = []
    for spec in CURATED:
        if spec.source_time:
            kst = to_kst(spec.source_date, spec.source_time, spec.source_timezone)
            date_kst = kst.date().isoformat()
            time_kst = kst.strftime("%H:%M")
        else:
            date_kst = spec.source_date
            time_kst = None
        out.append({
            "date": date_kst,
            "time": time_kst,
            "country": spec.country,
            "title": spec.title,
            "impact": spec.impact,
            "note": spec.note,
            "source": "curated",
            "source_date": spec.source_date,
            "source_time": spec.source_time,
            "source_timezone": spec.source_timezone,
            "source_url": spec.source_url,
        })
    return out
