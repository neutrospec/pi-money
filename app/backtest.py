"""Did these verdicts mean anything at the time?

Every other module answers what is true now. This one asks a harder question
and has to be built so it cannot flatter itself, because a backtest that leaks
is worse than none — it produces a number nobody can argue with.

Two separate leak surfaces, and only one of them was already solved:

* **The verdict side** replays through ``app.pit.Ledger``, which filters by
  observation date and freezes the clock. That work is done and tested.
* **The outcome side is new code.** Pairing a verdict at D with what happened
  by D+N is exactly where look-ahead walks back in, so the forward window is
  taken strictly after D by observation date, and a test asserts it.

What this deliberately does not do: adjust anything. The percentile cuts, the
stress threshold, the horizon — all declared, all evaluated, none tuned. A
backtest permitted to move its own thresholds is the second classifier this
project keeps refusing to build, wearing a lab coat.

Scope and the reasons for every exclusion are in ``docs/tasks/M7.md``. The
short version: sentiment and movers read the live cache, breadth has no data
for 99% of the window, and vintage mode equals observed until revisions accrue
after the 2026-08-29 backfill. So this controls look-ahead and **does not**
control revision leak, and every result says so.
"""
from __future__ import annotations

import json
from statistics import median

from app import db, pit


# The first day the Korean classifier reported all five components. One day
# earlier it is 4/5 with volatility pending, because kr_vkospi history begins
# 2023-10-10 and a distribution needs 60 observations. Measured, not chosen —
# a test re-derives it so a data change cannot silently move the window.
WINDOW_START = "2023-12-18"

# The benchmark each regime is evaluated against. A Korean verdict judged by
# the S&P would be measuring the wrong market.
BENCHMARK = {"korea": "^KS11", "us": "^GSPC"}

# What counts as stress: a low at least this far below D's close, within this
# many trading days after D. Declared, and declared together — the pair is the
# definition, and quoting either alone means nothing. 7%/20 sits at an 18.6%
# base rate over this window: an event, where 3%/20 (43.9%) would be the norm.
DRAWDOWN_PCT = 7.0
HORIZON_DAYS = 20

# Reported alongside, so one declared pair never carries the conclusion.
GRID_PCT = (3.0, 5.0, 7.0, 10.0)
GRID_DAYS = (10, 20, 40)

# The verdict that constitutes a warning about conditions. Not a sell signal —
# the whole point of the exercise is to find out whether it carries any
# information at all.
WARNING = "risk_off"


def trading_days(symbol: str, start: str, end: str) -> list[str]:
    return [
        point["date"] for point in db.get_index_points(symbol)
        if start <= point["date"] <= end
    ]


def replay_calendar(start: str, end: str) -> list[str]:
    """Every session either market held, not just the Korean one.

    Replaying on the KRX calendar alone and then grading the US verdict against
    the S&P silently loses both ends: 42 NYSE sessions get no verdict at all,
    and 21 verdict dates have no S&P bar and were dropped by a dict lookup
    without anyone counting them. That is how ``window.days: 655`` came to sit
    next to ``contingency.days: 615`` with nothing explaining the gap, and how
    17 US risk_off verdicts became 16 rows in the conditional table.
    """
    days = set()
    for symbol in BENCHMARK.values():
        days.update(trading_days(symbol, start, end))
    return sorted(days)


def run(start: str = WINDOW_START, end: str | None = None,
        mode: str = pit.OBSERVED, progress=None) -> int:
    """Replay every trading day in the window and cache the verdicts."""
    days = replay_calendar(start, end or "9999")
    rows = []
    for index, day in enumerate(days):
        result = pit.replay(day, mode)
        korea, us = result["korea_regime"], result["regime"]
        rows.append({
            "date": day,
            "mode": mode,
            "korea_regime": korea["regime"],
            "korea_score": korea["score"],
            "korea_active": korea["component_count"],
            "korea_components": json.dumps(
                {item["key"]: item["score"] for item in korea.get("components", [])},
                ensure_ascii=False,
            ),
            "us_regime": us["regime"],
            "us_score": us["score"],
        })
        if progress and index % 50 == 0:
            progress(index, len(days))
    return db.save_backtest_verdicts(rows)


def forward(symbol: str, horizon: int = HORIZON_DAYS) -> dict[str, dict]:
    """For each observation date, what the next ``horizon`` sessions did.

    The window is ``points[i + 1 : i + 1 + horizon]`` — strictly after D. This
    slice is the whole leak surface on the outcome side: including D's own
    close would let a verdict be graded partly on the bar it was made from,
    and starting anywhere earlier would grade it on the past.
    """
    points = db.get_index_points(symbol)
    values = [point["value"] for point in points]
    out = {}
    for index, point in enumerate(points):
        ahead = values[index + 1:index + 1 + horizon]
        if len(ahead) < horizon:
            # A truncated window is not a smaller window; it is a day whose
            # outcome has not happened yet. Reporting it as "no stress" would
            # count the unfinished present as evidence of calm.
            continue
        base = values[index]
        out[point["date"]] = {
            "base": base,
            "low": min(ahead),
            "last": ahead[-1],
            "drawdown_pct": round((min(ahead) / base - 1) * 100, 3),
            "return_pct": round((ahead[-1] / base - 1) * 100, 3),
        }
    return out


def contingency(verdicts: list[dict], outcomes: dict[str, dict], *,
                field: str, threshold: float = DRAWDOWN_PCT) -> dict:
    """Warnings against what followed, with the base rate that gives it meaning.

    Precision without the base rate is decoration. If stress follows 18.6% of
    days, a warning that is right 20% of the time has told you nothing, and
    the only way to see that is to print both.
    """
    # Dates the benchmark did not trade, or whose horizon has not finished.
    # Counted rather than dropped: a silent filter is how the US calendar
    # mismatch hid for as long as it did.
    unmatched = [row["date"] for row in verdicts if row["date"] not in outcomes]
    paired = [
        (row[field] == WARNING, outcomes[row["date"]]["drawdown_pct"] <= -threshold)
        for row in verdicts if row["date"] in outcomes
    ]
    hit = sum(1 for warned, stressed in paired if warned and stressed)
    false_alarm = sum(1 for warned, stressed in paired if warned and not stressed)
    miss = sum(1 for warned, stressed in paired if not warned and stressed)
    correct = sum(1 for warned, stressed in paired if not warned and not stressed)
    total = len(paired)
    stressed = hit + miss
    warned = hit + false_alarm
    return {
        "threshold_pct": threshold,
        "days": total,
        "ungraded": len(unmatched),
        "hit": hit,
        "false_alarm": false_alarm,
        "miss": miss,
        "correct_rejection": correct,
        "warned": warned,
        "stressed": stressed,
        # The share of days stress follows regardless of any verdict. Every
        # rate below has to be read against this one.
        "base_rate": round(stressed / total * 100, 1) if total else None,
        "precision": round(hit / warned * 100, 1) if warned else None,
        "recall": round(hit / stressed * 100, 1) if stressed else None,
        # Precision minus base rate: what the warning added over knowing
        # nothing. Zero or negative means the verdict carried no information
        # about this event, however respectable the precision looked.
        "lift": (
            round(hit / warned * 100 - stressed / total * 100, 1)
            if warned and total else None
        ),
    }


def grid(verdicts: list[dict], symbol: str, *, field: str) -> list[dict]:
    """The full threshold grid, so no single declared pair carries the result."""
    out = []
    for horizon in GRID_DAYS:
        outcomes = forward(symbol, horizon)
        for pct in GRID_PCT:
            table = contingency(verdicts, outcomes, field=field, threshold=pct)
            out.append({**table, "horizon_days": horizon,
                        "declared": pct == DRAWDOWN_PCT and horizon == HORIZON_DAYS})
    return out


def _spread(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}

    def at(share: float) -> float:
        return round(ordered[min(int(share * len(ordered)), len(ordered) - 1)], 2)

    return {
        "count": len(ordered),
        "median": round(median(ordered), 2),
        "p10": at(0.10), "p25": at(0.25), "p75": at(0.75), "p90": at(0.90),
        "worst": round(ordered[0], 2),
        "best": round(ordered[-1], 2),
    }


def conditional(verdicts: list[dict], outcomes: dict[str, dict], *, field: str) -> dict:
    """Forward returns and drawdowns grouped by verdict, without any threshold.

    This is the part that does not depend on a declared cut, which makes it the
    more informative half: if risk_off days and risk_on days have the same
    forward distribution, the classifier is not separating anything, and no
    choice of stress threshold can rescue that.
    """
    groups: dict[str, dict[str, list]] = {}
    ungraded: dict[str, int] = {}
    for row in verdicts:
        outcome = outcomes.get(row["date"])
        if not outcome:
            ungraded[row[field]] = ungraded.get(row[field], 0) + 1
            continue
        bucket = groups.setdefault(row[field], {"return": [], "drawdown": []})
        bucket["return"].append(outcome["return_pct"])
        bucket["drawdown"].append(outcome["drawdown_pct"])
    return {
        name: {"forward_return": _spread(values["return"]),
               "forward_drawdown": _spread(values["drawdown"]),
               # Verdicts of this kind that could not be graded at all. Without
               # it a reader compares a count here against the verdict table
               # and finds a discrepancy with no explanation.
               "ungraded": ungraded.get(name, 0)}
        for name, values in sorted(groups.items())
    }


def churn(verdicts: list[dict], *, field: str) -> dict:
    """How often the verdict changes, and how long it stays put.

    A classifier that flips every few days is unusable whatever its accuracy,
    because acting on it is impossible and reading it is exhausting. Run
    lengths say that plainly where an accuracy number never would.
    """
    runs, current, previous = [], 0, None
    for row in verdicts:
        if row[field] != previous:
            if previous is not None:
                runs.append({"regime": previous, "days": current})
            previous, current = row[field], 1
        else:
            current += 1
    if previous is not None:
        runs.append({"regime": previous, "days": current})
    lengths = [run["days"] for run in runs]
    return {
        "changes": max(0, len(runs) - 1),
        "runs": len(runs),
        "median_run_days": round(median(lengths), 1) if lengths else None,
        "shortest_run_days": min(lengths) if lengths else None,
        "longest_run_days": max(lengths) if lengths else None,
        "by_regime": {
            name: sum(1 for run in runs if run["regime"] == name)
            for name in sorted({run["regime"] for run in runs})
        },
    }


def timing(verdicts: list[dict], outcomes: dict[str, dict], *, field: str,
           threshold: float = DRAWDOWN_PCT) -> dict:
    """When a warning arrives relative to the stress it was warning about.

    A warning on the day the drawdown is already underway is not the same
    finding as one that arrives a week early, and a single accuracy number
    cannot tell them apart.
    """
    days = [row["date"] for row in verdicts]
    warned = {row["date"] for row in verdicts if row[field] == WARNING}
    index = {day: position for position, day in enumerate(days)}
    leads = []
    for day in days:
        outcome = outcomes.get(day)
        if not outcome or outcome["drawdown_pct"] > -threshold:
            continue
        # First warning at or before this day, within the same horizon.
        window = days[max(0, index[day] - HORIZON_DAYS):index[day] + 1]
        earlier = [item for item in window if item in warned]
        leads.append(index[day] - index[earlier[0]] if earlier else None)
    covered = [value for value in leads if value is not None]
    return {
        "stress_days": len(leads),
        "warned_within_horizon": len(covered),
        "unwarned": len(leads) - len(covered),
        "median_lead_days": round(median(covered), 1) if covered else None,
        "max_lead_days": max(covered) if covered else None,
    }


# The rule the composite verdict collapses into. `structure` shows that trend
# is a mandatory gate and that drawdown votes -1 on nearly every warning day,
# so risk_off is reachable in practice only when both price components are
# negative. Written out explicitly here for one purpose: it uses only ^KS11,
# which has twenty years of history, so it can be tested outside the window the
# full classifier is confined to. That is the difference between a regularity
# and an artifact of 2024-2026.
def price_rule(points: list[dict]) -> list[dict]:
    """Reproduce the two price components of the Korean verdict from ^KS11 alone.

    Faithful to ``analysis._kospi_trend`` (close vs the 200-session mean) and
    ``analysis._kospi_drawdown`` (distance below the 52-week high, percentile
    scored against the whole history of the same ratio, cut at 20/80). Both are
    computed point-in-time: at each index only values up to and including that
    day are used, so this carries the same no-look-ahead property the Ledger
    gives the real classifier.
    """
    from app import analysis, normalize

    values = [point["value"] for point in points]
    trend_window = analysis.KR_MIN_HISTORY["trend"]
    peak_window = analysis.KR_DRAWDOWN_WINDOW
    start = max(trend_window, peak_window, analysis.KR_MIN_HISTORY["drawdown"])
    ratios = [
        values[index] / max(values[index - peak_window:index + 1]) - 1
        for index in range(peak_window, len(values))
    ]
    out = []
    for index in range(start, len(values)):
        average = sum(values[index - trend_window + 1:index + 1]) / trend_window
        history = ratios[:index - peak_window + 1]
        percentile = normalize.percentile(history[-1], history)
        drawdown = (
            1 if percentile >= analysis.KR_RISK_ON_PERCENTILE
            else -1 if percentile <= analysis.KR_RISK_OFF_PERCENTILE else 0
        )
        out.append({
            "date": points[index]["date"],
            "trend": 1 if values[index] >= average else -1,
            "drawdown": drawdown,
            "warning": values[index] < average and drawdown == -1,
        })
    return out


def out_of_window(symbol: str = "^KS11", horizon: int = HORIZON_DAYS,
                  boundary: str = WINDOW_START) -> dict:
    """Run the price-only rule over twenty years and split at the window edge.

    The full classifier cannot go back before 2023-12-18 because kr_vkospi
    history begins 2023-10-10. The rule it collapses into can, and this is the
    one test available today that distinguishes "a real mean-reversion
    regularity" from "what deep-drawdown days happened to do during a +167%
    stretch". If the effect lives only after the boundary, it is the window.
    """
    points = db.get_index_points(symbol)
    outcomes = forward(symbol, horizon)
    rows = [row for row in price_rule(points) if row["date"] in outcomes]

    def summarise(subset: list[dict]) -> dict:
        warned = [row for row in subset if row["warning"]]
        returns = [outcomes[row["date"]]["return_pct"] for row in warned]
        every = [outcomes[row["date"]]["return_pct"] for row in subset]
        return {
            "days": len(subset),
            "warned": len(warned),
            "warned_median_return": round(median(returns), 2) if returns else None,
            "warned_positive_pct": (
                round(sum(value > 0 for value in returns) / len(returns) * 100, 1)
                if returns else None
            ),
            "warned_worst": round(min(returns), 2) if returns else None,
            # The number every rate above has to be read against.
            "all_days_median_return": round(median(every), 2) if every else None,
            "all_days_positive_pct": (
                round(sum(value > 0 for value in every) / len(every) * 100, 1)
                if every else None
            ),
        }

    before = [row for row in rows if row["date"] < boundary]
    after = [row for row in rows if row["date"] >= boundary]
    return {
        "symbol": symbol,
        "horizon_days": horizon,
        "boundary": boundary,
        "rule": "trend == -1 AND drawdown == -1 (가격만 사용)",
        "before": summarise(before),
        "after": summarise(after),
        "by_year": {
            year: summarise([row for row in rows if row["date"][:4] == year])
            for year in sorted({row["date"][:4] for row in rows})
        },
        "note": (
            "전체 분류기는 kr_vkospi 이력이 2023-10-10부터라 창 밖으로 못 나갑니다. "
            "그 분류기가 실제로 무너져 들어가는 가격 규칙은 나갈 수 있고, 그 규칙이 "
            "창 안에서만 통한다면 발견이 아니라 그 창의 성질입니다."
        ),
    }


def structure(verdicts: list[dict]) -> dict:
    """What the Korean classifier turned out to be, as opposed to what it declares.

    Korean only, because ``backtest_verdicts`` stores per-component votes for
    that classifier alone. The US verdict is cached as a regime and a score,
    so there is nothing here to decompose and this must not be called for it.

    A composite of five votes can collapse into one, and nothing in an accuracy
    number shows it. Two things this measures, both of which changed how the
    headline result has to be read:

    * **Components that never vote 0.** ``_kospi_trend`` returns +1 or -1 with
      no neutral band, unlike every other component, which goes through
      ``analysis._cut``. With five active components a risk_off needs a ratio
      of -0.5, so a risk_off while trend votes +1 would require all four others
      at -1 simultaneously — which never happens. Trend is therefore a
      mandatory gate rather than a vote, and the nominal five-input composite
      is a 200-day moving-average crossover with confirmation. The docstring of
      ``korea_regime`` says components vote -1/0/+1; the code cannot.
    * **Cuts that stop discriminating.** A vote fired on most days is not a
      signal. Reported per year because the failure is gradual: a percentile
      cut taken over a "full history" that is only 2.5 years long drifts as the
      level shifts under it.

    Measured, not fixed. Changing a component and re-scoring the same window is
    threshold tuning wearing a lab coat — the module docstring already refuses
    it, and that refusal has to hold when the finding is inconvenient.
    """
    votes: dict[str, list[int]] = {}
    for row in verdicts:
        for key, score in json.loads(row["korea_components"]).items():
            votes.setdefault(key, []).append(score)
    components = []
    for key, scores in sorted(votes.items()):
        counts = {value: scores.count(value) for value in (-1, 0, 1)}
        total = len(scores)
        components.append({
            "key": key,
            "votes": counts,
            "days": total,
            # A component with no neutral band cannot abstain, so it gates the
            # verdict instead of contributing to it.
            "degenerate": counts[0] == 0,
            "negative_share": round(counts[-1] / total * 100, 1) if total else None,
        })
    warned = [row for row in verdicts if row["korea_regime"] == WARNING]
    gates = [
        item["key"] for item in components
        if item["degenerate"] and all(
            json.loads(row["korea_components"]).get(item["key"]) == -1
            for row in warned
        )
    ] if warned else []
    by_year: dict[str, dict[str, dict]] = {}
    for row in verdicts:
        year = row["date"][:4]
        for key, score in json.loads(row["korea_components"]).items():
            cell = by_year.setdefault(year, {}).setdefault(key, {"negative": 0, "days": 0})
            cell["days"] += 1
            cell["negative"] += score == -1
    return {
        "components": components,
        # Components that are -1 on every single warning day and cannot vote 0.
        # These are not contributors; without them the verdict cannot fire.
        "mandatory_gates": gates,
        "negative_share_by_year": {
            year: {key: round(cell["negative"] / cell["days"] * 100, 1)
                   for key, cell in sorted(keys.items())}
            for year, keys in sorted(by_year.items())
        },
        "note": (
            "이 절은 분류기를 고치지 않고 그것이 실제로 무엇인지 잽니다. "
            "mandatory_gates 에 오른 구성요소는 표를 던지는 것이 아니라 "
            "판정의 관문입니다 — 그것이 -1 이 아니면 판정이 나올 수 없습니다."
        ),
    }


def report(field: str = "korea_regime") -> dict:
    """Everything above for one classifier, with its limits attached."""
    market = "korea" if field.startswith("korea") else "us"
    symbol = BENCHMARK[market]
    verdicts = db.get_backtest_verdicts()
    outcomes = forward(symbol, HORIZON_DAYS)
    if not verdicts:
        return {"available": False,
                "reason": "재생된 판정이 아직 없습니다. app.backtest.run 을 먼저 실행하세요."}
    return {
        "available": True,
        "field": field,
        "market": market,
        "benchmark": symbol,
        "window": {"start": verdicts[0]["date"], "end": verdicts[-1]["date"],
                   "days": len(verdicts)},
        "declared": {"drawdown_pct": DRAWDOWN_PCT, "horizon_days": HORIZON_DAYS,
                     "warning": WARNING},
        "contingency": contingency(verdicts, outcomes, field=field),
        "grid": grid(verdicts, symbol, field=field),
        "conditional": conditional(verdicts, outcomes, field=field),
        "churn": churn(verdicts, field=field),
        # Korea only. ``structure`` reads korea_components, and the US
        # classifier's inputs (VIX, IG spread, S&P trend) are not stored at
        # all — rendering them under a US heading would be Korean internals
        # wearing the wrong label.
        "structure": structure(verdicts) if market == "korea" else None,
        # The one test available today that separates a regularity from a
        # property of this particular window. Korea only: the rule is built
        # from ^KS11 and there is no equivalent for the US classifier.
        "out_of_window": out_of_window(symbol) if market == "korea" else None,
        "timing": timing(verdicts, outcomes, field=field),
        "limits": [
            "판정 " + str(len(verdicts)) + "일 중 채점 가능한 날은 "
            + str(contingency(verdicts, outcomes, field=field)["days"]) + "일입니다. "
            "차이는 벤치마크 휴장일과 아직 구간이 안 끝난 날입니다.",
            "낙폭은 종가 기준입니다 — index_prices 에 고가·저가가 없어 "
            "장중 저점을 쓸 수 없습니다. 모든 판정에 똑같이 적용되므로 순위와 "
            "lift 비교는 유효하지만, 선언한 7%가 장중 7%는 아닙니다.",
            "forward_drawdown 은 진입 대비 이후 최저 수준이라 값이 양수일 수 "
            "있습니다. 하락폭이 아니라 '이후 최악의 수준'입니다.",
            "선견 누출은 통제했습니다 — 판정은 Ledger 로 관측일을 자르고 "
            "결과 구간은 D 이후만 씁니다.",
            "개정 누출은 통제하지 않았습니다 — 원장이 2026-08-29 백필 기준이라 "
            "vintage 모드가 observed 와 같은 값을 냅니다.",
            "시장 심리·주간 이동·시장폭은 재생 대상이 아닙니다. 이유는 "
            "docs/tasks/M7.md 에 있습니다.",
            "임계값은 평가 대상이지 조정 대상이 아닙니다. 결과가 컷이 틀렸다고 "
            "말하더라도 코드가 스스로 바꾸지 않습니다.",
        ],
        "warning": (
            "이것은 판정이 과거에 어떤 정보를 담았는지에 대한 서술이지 "
            "매매 규칙이 아닙니다. 정밀도는 반드시 기저율과 함께 읽으세요."
        ),
    }
