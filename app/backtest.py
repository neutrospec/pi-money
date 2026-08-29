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


# The first day the Korean classifier produces a verdict at all — three of five
# components, the classifier's own declared minimum. Two days earlier it is 2/5
# and reports unknown. Both boundaries are measured, not chosen, and a test
# re-derives them so a data change cannot move the window silently.
#
# These moved once already. Before the 2026-08-30 history backfill they were
# both 2023-12-18, because the collectors fetched three years by default and
# nothing in the window was older. The providers held far more; see
# ``app.history_backfill``. The window went from 2.7 years to 19, which is the
# difference between a record containing no sustained bear market and one
# containing 2008, 2011, 2015, 2018, 2020 and 2022.
WINDOW_START = "2007-06-26"

# The first day all five vote. Between the two dates the verdict stands on
# three or four components with volatility pending, because kr_vkospi begins
# 2010-01-04 and a distribution needs 60 observations. This date moved once
# already, from 2010-04-01, when a day-of-week bug in the KRX walk was fixed
# and ~670 missing Fridays arrived. Those years are a
# legitimate verdict under the classifier's own rule and a *different* verdict
# from a five-component one, so results are segmented rather than pooled.
FULL_WINDOW_START = "2010-03-16"

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


STRATA = {
    "year": lambda day: day[:4],
    "half": lambda day: f"{day[:4]}H{1 if day[5:7] <= '06' else 2}",
    "quarter": lambda day: f"{day[:4]}Q{(int(day[5:7]) - 1) // 3 + 1}",
}


def episodes(verdicts: list[dict], *, field: str) -> list[list[str]]:
    """Contiguous runs of warning. The unit of evidence, and it is not the day.

    Warnings run for months and outcome windows overlap twenty sessions, so
    623 warned days are nowhere near 623 observations. Counting episodes is
    what keeps a single 2008 run from reading as hundreds of independent
    confirmations.
    """
    runs, previous = [], None
    for row in verdicts:
        if row[field] != WARNING:
            previous = None
            continue
        if previous is None:
            runs.append([])
        runs[-1].append(row["date"])
        previous = row["date"]
    return runs


def stratified(verdicts: list[dict], outcomes: dict[str, dict], *, field: str,
               threshold: float = DRAWDOWN_PCT) -> dict:
    """Lift measured inside each period, then combined — never pooled across.

    This exists because the pooled number lied. Over 2007-2026 the pooled lift
    was +13.8, which reads as "the warning roughly doubles the chance of
    stress". It does not. Warnings concentrate in years whose unconditional
    stress rate is two to four times the average — 164 warnings in 2008 when
    46% of all days preceded a drawdown — so pooling those days with days from
    calm years produces lift arithmetically even if the warning separates
    nothing *within* any year. Compare each warning against the base rate of
    the period it actually falls in and the effect collapses: +3.3 by year,
    +0.6 by quarter, -1.2 by half-year, against +13.8 pooled.

    A classifier that is switched on during bad years is not the same thing as
    one that identifies bad days, and only this function can tell them apart.
    """
    out = {}
    for name, bucket in STRATA.items():
        rows = []
        for label in sorted({bucket(row["date"]) for row in verdicts}):
            subset = [row for row in verdicts if bucket(row["date"]) == label]
            table = contingency(subset, outcomes, field=field, threshold=threshold)
            if table["days"]:
                rows.append({"stratum": label, **table})
        warned = sum(row["warned"] for row in rows)
        # Weighted by warnings, so a stratum contributes in proportion to how
        # much of the claim rests on it. The unweighted mean is reported too:
        # they disagree exactly when a few large strata carry everything.
        lifted = [row for row in rows if row["lift"] is not None and row["warned"]]
        out[name] = {
            "strata": len(rows),
            "with_warnings": len(lifted),
            "positive": sum(1 for row in lifted if row["lift"] > 0),
            "weighted_lift": (
                round(sum(row["lift"] * row["warned"] for row in lifted) / warned, 1)
                if warned else None
            ),
            "unweighted_lift": (
                round(sum(row["lift"] for row in lifted) / len(lifted), 1)
                if lifted else None
            ),
            "rows": rows if name == "year" else None,
        }
    runs = episodes(verdicts, field=field)
    hit = [
        run for run in runs
        if any(outcomes.get(day, {}).get("drawdown_pct", 0) <= -threshold for day in run)
    ]
    out["episodes"] = {
        "count": len(runs),
        "with_a_hit": len(hit),
        "longest_days": max((len(run) for run in runs), default=0),
        "note": (
            "증거의 단위는 날이 아니라 에피소드입니다. 경고는 몇 달씩 이어지고 "
            "결과 창은 20세션씩 겹치므로, 경고일 수를 독립 관측 수로 읽으면 "
            "한 번의 국면이 수백 번의 확인처럼 보입니다."
        ),
    }
    out["pooled_vs_stratified"] = (
        "합산 lift 는 '나쁜 해에 켜져 있었다'까지 점수로 쳐줍니다. "
        "층별 lift 는 '그 해 안에서 나쁜 날을 골랐다'만 점수로 칩니다. "
        "둘이 크게 벌어지면 믿을 것은 뒤쪽입니다."
    )
    return out


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
    """How long the warning had already been on when the stress registered.

    An earlier version searched back only ``HORIZON_DAYS`` and reported the
    median of what it found. The lead could then never exceed 20 by
    construction, and 62% of cases sat exactly at 20 — median equal to maximum
    equal to the cap is the signature of a censored measure being read as a
    central tendency. It looked like "the warning arrives 20 days early"; it
    meant "the search stopped there".

    This measures to the start of the warning run the day belongs to, with no
    cap, and reports the distribution rather than one number. A long lead here
    is not prescience: it says the verdict had been risk_off for months, which
    is what a verdict does inside a bear market.
    """
    days = [row["date"] for row in verdicts]
    index = {day: position for position, day in enumerate(days)}
    warned = {row["date"] for row in verdicts if row[field] == WARNING}
    # For each day, how long the current warning run has been going.
    running, age = {}, None
    for day in days:
        age = (age + 1 if age is not None else 0) if day in warned else None
        if age is not None:
            running[day] = age
    leads, unwarned = [], 0
    for day in days:
        outcome = outcomes.get(day)
        if not outcome or outcome["drawdown_pct"] > -threshold:
            continue
        if day in running:
            leads.append(running[day])
        else:
            unwarned += 1
    return {
        "stress_days": len(leads) + unwarned,
        "warned_on_the_day": len(leads),
        "unwarned": unwarned,
        # Sessions the warning had already been on. Not a forecast horizon.
        "run_age": _spread([float(value) for value in leads]),
        "note": (
            "이 값은 경고가 며칠 앞서 왔는지가 아니라, 스트레스가 잡힌 시점에 "
            "경고가 이미 며칠째 켜져 있었는지입니다. 약세장 안에서 판정이 계속 "
            "켜져 있는 것을 선행으로 읽으면 안 됩니다."
        ),
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

    This was the module's one designed defence against "the finding is a
    property of the window", and the 2026-08-30 backfill spent it. Before the
    backfill the classifier could not replay past 2023-12-18, so 2006-2023 was
    a genuine holdout for the price rule. Extending the replay to 2007 turned
    the holdout into the sample. The function now reports that it has nothing
    to say rather than returning zeros, because an empty ``before`` bucket
    reads as "no effect out of sample" and means "no out of sample".
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
    if not before:
        # The backfill bought a nineteen-year window by spending the holdout.
        # The price rule needs 252 sessions of peak window plus 200 of trend
        # before it can speak, and ^KS11 here starts 2006-09-04, so there is no
        # longer any history outside the replay window to test against.
        # Reporting empty statistics would read as "no effect out of sample"
        # when it means "no out of sample".
        return {
            "available": False,
            "symbol": symbol, "boundary": boundary, "rule": None,
            "reason": (
                f"{boundary} 이전에 이 규칙이 낼 수 있는 관측이 없습니다. "
                f"백필이 창을 19년으로 넓히면서 홀드아웃을 다 써버렸고 "
                f"^KS11 이력 자체가 {points[0]['date']}부터입니다. 되살리려면 "
                f"KRX 에서 2006년 이전 지수를 직접 받아야 합니다 — Yahoo 는 "
                f"20년 롤링이라 닿지 않습니다."
            ),
        }
    return {
        "available": True,
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


def by_completeness(verdicts: list[dict], outcomes: dict[str, dict], *,
                    field: str) -> list[dict]:
    """Split the record by how many components were actually voting.

    A longer window is bought at the cost of a uniform one. The spread series
    reach back to 2006 and kr_vkospi only to 2010, so the early years run with
    volatility pending — a legitimate verdict under the classifier's own
    minimum, and a *different* verdict from a five-component one. Mixing them
    into one accuracy number would let a four-component era silently dilute or
    flatter the five-component one, which is the same averaging-away this
    project rejects everywhere else.
    """
    groups: dict[int, list[dict]] = {}
    for row in verdicts:
        groups.setdefault(row["korea_active"], []).append(row)
    out = []
    for active, rows in sorted(groups.items(), reverse=True):
        table = contingency(rows, outcomes, field=field)
        out.append({
            "active_components": active,
            "days": len(rows),
            "start": rows[0]["date"],
            "end": rows[-1]["date"],
            "contingency": table,
            "conditional": conditional(rows, outcomes, field=field),
        })
    return out


# From which date the recent-period caveat is measured. Declared rather than
# chosen after the fact: it is the year the deep-history record stops showing
# any hits, not a boundary picked to make a number look a particular way.
RECENT_FROM = "2023-01-01"


def _recent_caveat(verdicts: list[dict], outcomes: dict[str, dict], *,
                   field: str) -> dict | None:
    """What the warning has actually done lately, in one sentence.

    A backtest read once and forgotten is not much use if the screen it sits
    beside keeps showing a warning that the same backtest says is inverted.
    This travels with the result so the caveat reaches the live reader.
    """
    recent = [row for row in verdicts if row["date"] >= RECENT_FROM]
    warned = [row for row in recent
              if row[field] == WARNING and row["date"] in outcomes]
    if not warned:
        return None
    returns = [outcomes[row["date"]]["return_pct"] for row in warned]
    hits = sum(1 for row in warned
               if outcomes[row["date"]]["drawdown_pct"] <= -DRAWDOWN_PCT)
    return {
        "since": RECENT_FROM,
        "warnings": len(warned),
        "hits": hits,
        "median_forward_return": round(median(returns), 2),
        "negative_forward_returns": sum(1 for value in returns if value < 0),
        "text": (
            f"{RECENT_FROM} 이후 경고 {len(warned)}건의 적중은 {hits}건이고, "
            f"전방 {HORIZON_DAYS}세션 수익률 중앙값은 "
            f"{median(returns):+.2f}%입니다"
            + (" — 음수인 경우가 하나도 없습니다."
               if not any(value < 0 for value in returns) else ".")
            + " 지금 화면의 경고를 하락 근거로 삼지 마세요."
        ),
    }


def _standing_line(verdicts: list[dict], pooled: dict, layered: dict) -> str:
    """One sentence, built from the record rather than written down.

    Written from data so it cannot drift away from what the backtest says. A
    record with no warnings at all gets a different sentence — quoting a lift
    of None as a finding would be worse than saying nothing.
    """
    head = (
        f"이 판정은 하락 예측이 아닙니다. {verdicts[0]['date'][:4]}년 이후 "
        f"{len(verdicts)}거래일을 같은 코드로 재생해 검증했습니다"
    )
    lifts = {name: layered[name]["weighted_lift"] for name in ("year", "quarter", "half")}
    if pooled["lift"] is None or any(value is None for value in lifts.values()):
        return head + " — 아직 위험 회피 판정이 나온 적이 없어 적중을 잴 수 없습니다."
    return (
        head + ": 위험 회피 판정은 그 해 안에서 위험한 날을 고르지 못했습니다 — "
        f"층별 lift 연 {lifts['year']:+.1f} / 분기 {lifts['quarter']:+.1f} / "
        f"반기 {lifts['half']:+.1f}. (합산 {pooled['lift']:+.1f}은 위험했던 해에 "
        f"켜져 있던 것까지 점수로 친 값이라 믿을 수 없습니다.)"
    )


# Memoised because this line goes on every screen that shows a verdict and
# the stratified pass costs ~180ms. Keyed on the cache's own shape, so a
# replay invalidates it and nothing else has to remember to.
_NOTE_CACHE: dict[tuple, dict | None] = {}


def verdict_note(field: str = "korea_regime") -> dict | None:
    """What the screens showing this verdict have to say about it, always.

    A backtest that lives only on its own page is decoration. The brief, the
    front page and the layer cards render ``risk_off`` in red, which is where
    someone might act on it, so the finding has to travel there.

    Two levels, because two things are true at once. The standing line states
    what the verification found and is shown whatever today's verdict is —
    a caveat that appears only on warning days teaches the reader that quiet
    days are validated, which is the opposite of what was measured. The urgent
    line is added when the verdict *is* a warning right now, because that is
    the moment someone might mistake it for a forecast.

    Everything here is derived from the cached record rather than written down,
    so it cannot drift away from what the backtest actually says.
    """
    verdicts = db.get_backtest_verdicts()
    if not verdicts:
        return None
    key = (field, len(verdicts), verdicts[-1]["date"], verdicts[-1]["computed_at"])
    if key in _NOTE_CACHE:
        return _NOTE_CACHE[key]
    market = "korea" if field.startswith("korea") else "us"
    outcomes = forward(BENCHMARK[market], HORIZON_DAYS)
    pooled = contingency(verdicts, outcomes, field=field)
    layered = stratified(verdicts, outcomes, field=field)
    warning_now = verdicts[-1][field] == WARNING
    urgent = _recent_caveat(verdicts, outcomes, field=field) if warning_now else None
    note = {
        "field": field,
        "warning_now": warning_now,
        "window": f"{verdicts[0]['date']} ~ {verdicts[-1]['date']}",
        "standing": _standing_line(verdicts, pooled, layered),
        "urgent": urgent["text"] if urgent else None,
        "link": "/backtest",
    }
    _NOTE_CACHE.clear()
    _NOTE_CACHE[key] = note
    return note


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
        # Always beside the pooled number, never instead of it — the gap
        # between them is the finding.
        "stratified": stratified(verdicts, outcomes, field=field),
        "grid": grid(verdicts, symbol, field=field),
        "conditional": conditional(verdicts, outcomes, field=field),
        "churn": churn(verdicts, field=field),
        # Korea only. ``structure`` reads korea_components, and the US
        # classifier's inputs (VIX, IG spread, S&P trend) are not stored at
        # all — rendering them under a US heading would be Korean internals
        # wearing the wrong label.
        "structure": structure(verdicts) if market == "korea" else None,
        # Never one number across eras with different evidence behind them.
        "by_completeness": (
            by_completeness(verdicts, outcomes, field=field)
            if market == "korea" else None
        ),
        # The one test available today that separates a regularity from a
        # property of this particular window. Korea only: the rule is built
        # from ^KS11 and there is no equivalent for the US classifier.
        "out_of_window": out_of_window(symbol) if market == "korea" else None,
        "timing": timing(verdicts, outcomes, field=field),
        # A caveat that has to reach whoever is looking at a live warning, not
        # only whoever reads the backtest. Since 2023 the warning has been
        # inverted, and a reader who sees risk_off on the brief today has no
        # other way to learn that.
        "recent_caveat": _recent_caveat(verdicts, outcomes, field=field),
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
