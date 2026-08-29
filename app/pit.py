"""What this repository could have answered on a past date.

A verdict is only worth something if it would have said the same thing at the
time. Checking that needs two different filters, and confusing them is how a
backtest flatters itself:

``observed``
    Use only observations dated on or before the day. Catches look-ahead —
    reading Friday's close into Wednesday's reading. Every table supports it.

``vintage``
    Use only values we had actually *received* by that instant. Catches
    revision leak — the number for a date changing after the date. Only
    ``indicator_vintages`` supports it, and only from 2026-08-23 when the
    ledger began recording.

The gap between the two is the measurement. A replay whose two modes agree on
a series has no revision leak in that series; one that disagrees has exactly
that, and the size of the disagreement is the size of the leak.

The seam is a ``ledger`` keyword defaulting to None on the functions that
read. None means live, so no existing call site changes. It is one object
rather than a parameter threaded through ten signatures because this project
has already learned what happens when each call site chooses its own reading
policy — see the module docstring of ``app.normalize``.

Not reconstructible, and this module says so rather than pretending:

* Index prices. ``replace_index_points`` deletes and reinserts a symbol under
  one fresh timestamp, so every row carries the stamp of the last rebuild.
  Trend, drawdown, momentum, safe-haven and the S&P 200-day average therefore
  replay by observation date only.
* KRX bulk tables. ``market_daily`` overwrites ``retrieved_at`` on
  re-collection, so the rows we hold for a past session are not provably the
  rows we held then.
* Anything the catalogue itself changed — a series added, a direction
  declared, an explanation written. The code is today's code.
"""
from __future__ import annotations

from datetime import date

from app import db
from app.timeutil import kst_today


# The ledger's first write. Before this, vintage mode has nothing to say.
LEDGER_BEGAN = "2026-08-23"

OBSERVED = "observed"
VINTAGE = "vintage"

# Where a series' values came from in a replay.
FROM_VINTAGE = "vintage"        # values we had received by then
FROM_OBSERVED = "observed"      # today's values, filtered to dates on or before
UNAVAILABLE = "unavailable"     # nothing survives the filter


# Every ``retrieved_at`` in the ledger is stored as a UTC ISO string, and the
# cut is a string comparison in SQL. So the cut must be *written* in that same
# shape or the comparison is lexicographic nonsense: the KST-midnight spelling
# `2026-08-28T00:00:00+09:00` sorts after `2026-08-27T17:42:09+00:00`, which is
# the US close — nine hours of the future waved through. That was 25 rows on a
# single day, all of them US sessions that had not happened yet in Korea.
UTC_SUFFIX = "+00:00"


def _instant(day: str) -> str:
    """End of that KST day, written as the UTC instant the ledger stores.

    A same-day re-collection changes the value without changing the
    observation date, so the cut has to be an instant. Gold's 2026-08-28 close
    was 4639.60 at 06:46Z and 4505.70 at 20:49Z.

    KST midnight is 15:00Z the same calendar day. Spelling it that way makes
    string order equal instant order, which is what the query actually needs.
    """
    date.fromisoformat(day)     # reject a malformed day here, not in SQL
    return f"{day}T15:00:00{UTC_SUFFIX}"


class Ledger:
    """The reads a replay is allowed to make, and a record of what it used."""

    def __init__(self, as_of: str, mode: str = OBSERVED) -> None:
        if mode not in (OBSERVED, VINTAGE):
            raise ValueError(f"unknown mode: {mode}")
        self.as_of = as_of
        self.mode = mode
        self.instant = _instant(as_of)
        # key -> vintage | observed | unavailable. Written as it serves, so a
        # caller can report what each number actually stood on.
        self.provenance: dict[str, str] = {}

    # -- reads ------------------------------------------------------------
    def indicator(self, key: str) -> list[dict]:
        if self.mode == VINTAGE:
            points = db.get_indicator_vintage_points(
                key, as_of=self.instant, end=self.as_of
            )
            self.provenance[key] = FROM_VINTAGE if points else UNAVAILABLE
            return [
                {"date": item["date"], "value": item["value"]} for item in points
            ]
        points = db.get_indicator_points(key, end=self.as_of)
        self.provenance[key] = FROM_OBSERVED if points else UNAVAILABLE
        return points

    def index(self, symbol: str) -> list[dict]:
        """Always observation-date only; index prices carry no vintage."""
        points = db.get_index_points(symbol, end=self.as_of)
        self.provenance[symbol] = FROM_OBSERVED if points else UNAVAILABLE
        return points

    def spread(self, left: str, right: str) -> list[dict]:
        """A computed spread, both legs read through the same filter."""
        from app import market_metrics

        if self.mode == OBSERVED:
            series = market_metrics.aligned_spread_series(left, right)
            out = [item for item in series if item["date"] <= self.as_of]
            self.provenance[f"{left}-{right}"] = (
                FROM_OBSERVED if out else UNAVAILABLE
            )
            return out
        left_points = {item["date"]: item["value"] for item in self.indicator(left)}
        right_points = {item["date"]: item["value"] for item in self.indicator(right)}
        out = [
            {"date": day, "value": round(left_points[day] - right_points[day], 3)}
            for day in sorted(left_points.keys() & right_points.keys())
        ]
        self.provenance[f"{left}-{right}"] = FROM_VINTAGE if out else UNAVAILABLE
        return out

    def breadth(self) -> dict:
        from app import market_metrics

        return market_metrics.krx_breadth_snapshot(day=self.as_of)

    def today(self) -> date:
        """The wall clock, frozen. Freshness gates must not see the real one."""
        return date.fromisoformat(self.as_of)


def live_today(ledger: "Ledger | None") -> date:
    return ledger.today() if ledger else kst_today()


def coverage(as_of: str, keys: list[str]) -> dict:
    """How much of a replay would actually stand on vintage.

    Presence is not enough. The ledger holds five VKOSPI rows for 2026-08-26
    where the series itself has seven hundred; calling that "covered" would
    let a replay claim a distribution it does not have. Each series is
    therefore measured against the same minimum the live reading uses, and a
    series that falls short is reported as thin rather than as available.
    """
    from app import normalize

    ledger = Ledger(as_of, VINTAGE)
    rows = []
    for key in keys:
        points = ledger.indicator(key)
        needed = normalize.minimum_for(key)
        rows.append({
            "key": key,
            "observations": len(points),
            "minimum": needed,
            "usable": len(points) >= needed,
        })
    usable = [item for item in rows if item["usable"]]
    return {
        "as_of": as_of,
        "ledger_began": LEDGER_BEGAN,
        "requested": len(keys),
        "usable": len(usable),
        "thin": len(rows) - len(usable),
        "series": rows,
        # A replay standing on fewer inputs than the live reading is a partial
        # verdict, and the caller has to be able to see that before quoting it.
        "complete": len(usable) == len(rows),
    }


# The inputs the two regime verdicts stand on. Kept here so `coverage` and
# `leak` ask about exactly what the verdict used, not a hand-kept list.
REGIME_INPUTS = [
    "kr_vkospi", "kr_corp_bond_3y", "kr_treasury_3y",
    "kr_cp_91d", "kr_cd_91d", "us_vix", "us_ig_spread",
]


def replay(as_of: str, mode: str = OBSERVED) -> dict:
    """The verdicts this repository could have produced on ``as_of``.

    Runs the same classifier code against a ledger instead of the live
    database, with the clock frozen so freshness gates judge the day being
    replayed rather than today.
    """
    from app import brief

    ledger = Ledger(as_of, mode)
    korea = brief._korea_regime(ledger)
    us = brief._us_regime(ledger)
    return {
        "as_of": as_of,
        "mode": mode,
        "korea_regime": korea,
        "regime": us,
        # Arithmetic on the votes already replayed, so it costs a call and
        # covers the half of the brief that says what would change the answer.
        "flip_conditions": brief._flip_conditions(korea),
        # Said plainly rather than left as a gap the reader has to notice:
        # these read the live cache and are not part of the replay.
        "not_replayed": [
            {"section": "sentiment", "reason": "게이지는 라이브 캐시를 읽습니다"},
            {"section": "movers", "reason": "주간 이동은 라이브 캐시를 읽습니다"},
        ],
        "breadth_as_of": ledger.breadth().get("as_of"),
        "provenance": dict(ledger.provenance),
        "coverage": coverage(as_of, REGIME_INPUTS) if mode == VINTAGE else None,
        "warning": (
            "기록이 말하는 그날의 판정입니다. observed 모드는 관측일만 걸러 "
            "선견 누출을 막고, 값의 개정까지 되돌리지는 않습니다."
            if mode == OBSERVED else
            "받았던 값만으로 재생했습니다. 빈티지 원장이 얇은 계열은 "
            "coverage 에서 thin 으로 보고됩니다 — 부분 판정을 완전한 판정으로 "
            "인용하지 마세요."
        ),
    }


def _revisions(observed: dict, vintaged: dict, coverage_report: dict) -> dict:
    """Values that changed after the fact, separated from values we never had.

    A thin ledger and a revised value look identical in a naive diff — both
    make the two modes disagree — and calling the first one "leak" is the same
    mistake this module was built to catch, committed inside the leak report.
    So a series is compared only where both modes actually voted, and the
    verdicts are compared only when the vintage side stood on full coverage.
    """
    changed, skipped = [], []
    theirs = {item["key"]: item for item in observed["korea_regime"].get("components", [])}
    for item in vintaged["korea_regime"].get("components", []):
        mine = theirs.get(item["key"])
        if mine is None:
            continue
        if mine.get("percentile") != item.get("percentile"):
            changed.append({
                "key": item["key"], "label": item.get("label"),
                "observed": mine.get("percentile"),
                "vintage": item.get("percentile"),
            })
    for item in vintaged["korea_regime"].get("pending", []):
        skipped.append({"key": item.get("key"), "reason": item.get("reason")})
    verdicts = []
    if coverage_report.get("complete"):
        for name in ("korea_regime", "regime"):
            left, right = observed[name], vintaged[name]
            if left.get("regime") != right.get("regime"):
                verdicts.append({
                    "verdict": name,
                    "observed": f"{left.get('regime')} ({left.get('score')})",
                    "vintage": f"{right.get('regime')} ({right.get('score')})",
                })
    return {
        "components": changed,
        "verdicts": verdicts,
        # Not "no revision found" — not looked at. The distinction is the
        # whole point of reporting it.
        "not_compared": skipped,
        "verdict_comparable": bool(coverage_report.get("complete")),
    }


# Components whose inputs carry no vintage at all, so revision can never show
# up in them. Reporting them as clean would claim a check that never ran.
UNCHECKABLE = ("trend", "drawdown")


def leak(as_of: str) -> dict:
    """Measure what a replay of ``as_of`` would borrow from the future.

    Two borrowings, and they need different instruments:

    ``revision``
        observed against vintage on the same date. A difference is a value
        that changed after the fact — a live reading would use the corrected
        number, and the day itself could not have.
    ``identical_to_live``
        components whose replayed percentile equals today's. They *may* simply
        not have moved; they may also be a read that ignored the date filter.
        Reported as something to check, not as a finding.
    """
    from app import brief

    observed = replay(as_of, OBSERVED)
    vintaged = replay(as_of, VINTAGE)
    report = vintaged["coverage"]
    live = brief._korea_regime()
    live_parts = {item["key"]: item for item in live.get("components", [])}
    frozen = [
        {"key": item["key"], "label": item.get("label"),
         "percentile": item.get("percentile")}
        for item in observed["korea_regime"].get("components", [])
        # A component with no percentile — trend, drawdown — would compare
        # None to None and match every time, which is a bug that reads as a
        # finding. Those are reported under ``unchecked`` instead.
        if item.get("percentile") is not None
        and (other := live_parts.get(item["key"]))
        and other.get("percentile") == item.get("percentile")
    ]
    return {
        "as_of": as_of,
        "revision": _revisions(observed, vintaged, report),
        "identical_to_live": frozen,
        "unchecked": [
            {"key": key,
             "reason": "지수 가격에는 빈티지가 없어 개정 누출을 확인할 수 없습니다"}
            for key in UNCHECKABLE
        ],
        "coverage": report,
        "note": (
            "identical_to_live 는 발견이 아니라 확인 대상입니다. 값이 정말 안 "
            "움직였을 수도, 날짜 필터를 무시한 읽기가 남았을 수도 있습니다. "
            "unchecked 는 '깨끗함'이 아니라 '보지 못함'입니다."
        ),
    }


def readiness(keys: list[str] | None = None, *, today: date | None = None) -> dict:
    """From which date each series can be replayed — a fact, not a projection.

    "1 of 7 usable" is a true status and a useless one: it says the answer is
    no without saying when it becomes yes. But the answer is not a forecast
    either, because most of this ledger arrived in bulk. 98% of its rows were
    backfilled rather than received on the day they describe, and a backfill
    does something precise — it makes every date *after* it replayable at full
    depth, and no date before it replayable at all.

    So the question "when will there be enough" has an exact answer already in
    the ledger: the instant its Nth observation arrived, where N is the minimum
    that series needs. Reporting an accrual rate instead would turn a fact into
    an estimate and get it wrong in both directions — too pessimistic for the
    series already backfilled, too optimistic for the ones nothing is feeding.
    """
    from app import normalize

    when = today or kst_today()
    rows = []
    for key in keys or REGIME_INPUTS:
        arrivals = db.vintage_arrivals(key)
        needed = normalize.minimum_for(key)
        # The instant depth first reached the minimum is simply the arrival of
        # the Nth observation. Everything from that day onward replays.
        reached = arrivals[needed - 1][:10] if len(arrivals) >= needed else None
        rows.append({
            "key": key,
            "observations": len(arrivals),
            "minimum": needed,
            # Held now, and separately: replayable as of today. A series
            # backfilled this morning is the second but was not yesterday.
            "replayable_from": reached,
            "usable_today": bool(reached and reached <= when.isoformat()),
            "first_arrival": arrivals[0][:10] if arrivals else None,
            "note": (
                f"{reached}부터 재생 가능합니다" if reached
                else f"관측 {len(arrivals)}개 — {needed}개에 아직 못 미칩니다"
                if arrivals else "원장이 이 계열을 아직 받지 못했습니다"
            ),
        })
    reached = [item["replayable_from"] for item in rows if item["replayable_from"]]
    return {
        "as_of": when.isoformat(),
        "ledger_began": LEDGER_BEGAN,
        "usable": sum(1 for item in rows if item["usable_today"]),
        "requested": len(rows),
        # The last series to arrive sets when a *full* replay becomes possible.
        # None while any series is still short — a date computed from the ones
        # that made it would describe a replay nobody can run.
        "complete_from": (
            max(reached) if len(reached) == len(rows) else None
        ),
        "waiting": [item["key"] for item in rows if not item["replayable_from"]],
        "series": rows,
        "method": (
            "원장의 N번째 관측이 도착한 시각이 곧 깊이가 채워진 시각입니다. "
            "이 원장은 98%가 사후 백필이라 속도로 추정하면 이미 채워진 계열은 "
            "너무 비관적으로, 수집이 멈춘 계열은 너무 낙관적으로 나옵니다."
        ),
    }
