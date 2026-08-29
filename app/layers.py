"""The five layers a reading actually rests on, each answering one question.

The brief already groups evidence by *kind of disagreement*. That is the right
axis for contradictions and the wrong one for building a picture: it tells you
that two things conflict without saying what part of the economy they conflict
about. This module cuts the same catalogue the other way — by what the series
is evidence *of* — so "정책은 완화, 신용은 조임" is a sentence the system can
produce rather than one the reader has to assemble.

Five layers, not fifteen. The catalogue's analysis groups are the right
granularity for coverage accounting and too fine for a screen: a card per group
is a table, and a table is what the reader was already failing to read.

The scoring is `normalize`'s and the cuts are `analysis`'s. A layer is a
different question asked of the same distribution readings, not a second
opinion about where a value sits — this project has already paid for having
two of those.

Four of the five layers vote; the policy layer does not, and the difference is
deliberate. Every policy-rate and inflation series in the catalogue declares
its direction as ``neutral`` — a rate rise is tightening or it is a recovering
economy, and which one depends on why, which a percentile cannot see. Forcing
a risk verdict out of them would reintroduce exactly the confusion that
declaration was written to prevent. So the policy card reports level and
travel, says it is not voting, and says why.

Confidence is derived here rather than asserted. A verdict standing on four
stale series out of eleven is a weaker claim than the same verdict standing on
eleven fresh ones, and saying so in a number is the difference between a screen
that reports and one that admits.
"""
from __future__ import annotations

from datetime import date

from app import analysis, normalize
from app.collectors import indicators
from app.timeutil import kst_today


# Each layer names the question it answers and what a high reading means for
# it. Without that second sentence "정책 risk_on" is a label; with it, it is a
# statement someone meeting the screen for the first time can check.
LAYERS = (
    {
        "key": "policy",
        "label": "정책",
        "groups": ("policy_rates", "inflation"),
        "mode": "level",
        "question": "돈의 값은 어디쯤이고 어느 쪽으로 움직이는가",
        "abstains": (
            "금리와 물가는 위험 방향을 선언하지 않습니다. 금리 상승은 긴축일 "
            "수도 경기 회복일 수도 있고 어느 쪽인지는 분포가 답할 수 없습니다. "
            "그래서 이 층은 판정하지 않고 수준과 이동만 보고합니다."
        ),
    },
    {
        "key": "growth",
        "label": "경기",
        "groups": ("growth_cycle", "labor", "trade_semiconductors", "housing"),
        "mode": "stance",
        "question": "실물 활동은 강해지는 중인가 약해지는 중인가",
        "risk_on": "생산·고용·수출이 자기 분포의 위쪽에 있습니다",
        "risk_off": "생산·고용·수출이 자기 분포의 아래쪽에 있습니다",
    },
    {
        "key": "liquidity",
        "label": "유동성",
        "groups": ("liquidity", "fx_external"),
        "mode": "stance",
        "question": "돈은 얼마나 있고 얼마나 쉽게 움직이는가",
        "risk_on": "통화량과 대외 여건이 자금 조달에 우호적인 쪽입니다",
        "risk_off": "통화량과 대외 여건이 자금 조달을 압박하는 쪽입니다",
    },
    {
        "key": "credit",
        "label": "신용",
        "groups": ("credit_stress", "volatility"),
        "mode": "stance",
        "question": "위험의 값은 싸게 매겨지는가 비싸게 매겨지는가",
        "risk_on": "스프레드와 변동성이 낮아 위험을 싸게 매기고 있습니다",
        "risk_off": "스프레드와 변동성이 높아 위험을 비싸게 매기고 있습니다",
    },
    {
        "key": "breadth",
        "label": "시장폭",
        "groups": ("market_breadth", "sentiment", "positioning",
                   "valuation", "commodities"),
        "mode": "stance",
        "question": "오르는 것은 넓은가 좁은가",
        "risk_on": "참여가 넓고 심리·포지션이 위험선호 쪽입니다",
        "risk_off": "참여가 좁고 심리·포지션이 위험회피 쪽입니다",
    },
)

# Every group must land in exactly one layer, checked rather than assumed —
# a group added to the catalogue and forgotten here would vanish from the
# screens silently, which is the failure mode this project keeps finding.
GROUPS_BY_LAYER = {item["key"]: set(item["groups"]) for item in LAYERS}

# How many series a card shows before it stops being read. The rest still
# count toward the vote and the confidence; only the display is capped.
EVIDENCE_LIMIT = 6


def _vote(risk_percentile: float | None) -> int | None:
    """The same three-way cut the regime classifiers use, on the same scale."""
    if risk_percentile is None:
        return None
    if risk_percentile >= analysis.KR_RISK_ON_PERCENTILE:
        return 1
    if risk_percentile <= analysis.KR_RISK_OFF_PERCENTILE:
        return -1
    return 0


def _staleness(spec: dict, as_of: str | None, today: date) -> int | None:
    """Days past this series' own allowance, or None if it is current.

    The allowance is per series because the cycles differ: a daily series
    silent for a week is broken, a quarterly one is on time.
    """
    if not as_of:
        return None
    try:
        age = (today - date.fromisoformat(as_of)).days
    except ValueError:
        return None
    allowance = spec.get("max_age_days") or normalize.LOOKBACK_DAYS
    return age - allowance if age > allowance else None


def _evidence(key: str, spec: dict, today: date, *, travel: bool = False) -> dict:
    reading = normalize.position_for(key)
    direction = indicators.risk_direction(key)
    if not reading.get("available"):
        return {
            "key": key, "label": spec["label"], "voted": False,
            "reason": reading.get("reason", "분포 판정 불가"),
        }
    late = _staleness(spec, reading.get("as_of"), today)
    # Only the level card asks for this; it is a second full read of the
    # series and the voting cards do not use it.
    moved = normalize.movement_for(key) if travel else None
    return {
        "movement": (
            {"change": moved["change"], "from_date": moved["from_date"]}
            if moved else None
        ),
        "key": key,
        "label": spec["label"],
        "unit": spec["unit"],
        "voted": True,
        "value": reading["value"],
        "as_of": reading["as_of"],
        "percentile": reading["percentile"],
        "risk_percentile": reading["risk_percentile"],
        "direction": direction,
        "vote": _vote(reading["risk_percentile"]),
        "priority": spec["priority"],
        "window_label": reading["window_label"],
        # Days past its own allowance, not days old. A quarterly series three
        # weeks after the quarter is on time and must not read as late.
        "days_late": late,
        "stale": late is not None,
    }


def confidence(evidence: list[dict], expected: int) -> dict:
    """How much of the expected evidence actually spoke, and how fresh it was.

    Two ways a reading gets weaker and they are not the same. Evidence that
    never arrived shrinks the base the verdict stands on. Evidence that arrived
    late is still evidence, but of an earlier world. Reported apart, because
    the fixes are different: one is a collector problem, the other is a
    publication schedule nobody can change.
    """
    voted = [item for item in evidence if item["voted"]]
    stale = [item for item in voted if item["stale"]]
    fresh = len(voted) - len(stale)
    reported = len(voted) / expected if expected else 0.0
    # Stale evidence counts half. Not a tuned weight — a stated one: a value
    # past its own allowance is worth having and not worth as much as a
    # current one, and picking 0 would throw away the only reading we have.
    strength = (fresh + 0.5 * len(stale)) / expected if expected else 0.0
    level = (
        "high" if strength >= 0.7
        else "medium" if strength >= 0.4
        else "low"
    )
    reasons = []
    if len(voted) < expected:
        reasons.append(
            f"근거 {expected}개 중 {expected - len(voted)}개가 분포 판정에 "
            f"필요한 이력을 아직 못 채웠습니다"
        )
    if stale:
        worst = max(stale, key=lambda item: item["days_late"])
        reasons.append(
            f"{len(stale)}개가 자기 갱신 주기를 넘겼습니다 "
            f"(가장 늦은 것은 {worst['label']}, {worst['days_late']}일 초과)"
        )
    return {
        "level": level,
        "strength": round(strength, 3),
        "reported": round(reported, 3),
        "expected": expected,
        "voted": len(voted),
        "fresh": fresh,
        "stale": len(stale),
        "reasons": reasons,
        "method": (
            "보고된 근거 비율에서 갱신 주기를 넘긴 근거를 절반으로 세어 "
            "계산합니다. 판정을 바꾸지 않고, 그 판정이 얼마나 두꺼운 "
            "근거 위에 서 있는지만 말합니다."
        ),
    }


def _stance(votes: list[int]) -> tuple[str, int]:
    if not votes:
        return "unknown", 0
    score = sum(votes)
    ratio = score / len(votes)
    if ratio >= 0.5:
        return "risk_on", score
    if ratio <= -0.5:
        return "risk_off", score
    return "neutral", score


def layer(spec: dict, catalog: dict, today: date) -> dict:
    """One evidence card: what this part of the economy is saying, and how well."""
    groups = set(spec["groups"])
    votes = spec["mode"] == "stance"
    members = [
        (key, item) for key, item in catalog.items()
        if item["analysis_group"] in groups
        # A series whose rise has no agreed meaning cannot vote on a stance.
        # Counting it as expected would permanently depress every confidence
        # score for evidence that was never going to arrive. The level card
        # has no such filter — an unpolarised series is its whole subject.
        and (
            not votes
            or indicators.risk_direction(key) in (indicators.RISK, indicators.SUPPORT)
        )
    ]
    evidence = [_evidence(key, item, today, travel=not votes) for key, item in members]
    voted = [item for item in evidence if item["voted"]]
    if votes:
        stance, score = _stance([item["vote"] for item in voted])
        reading = (
            spec["risk_on"] if stance == "risk_on"
            else spec["risk_off"] if stance == "risk_off"
            else "근거가 한쪽으로 모이지 않습니다" if stance == "neutral"
            else "판정할 근거가 아직 없습니다"
        )
    else:
        stance, score, reading = "abstains", 0, spec["abstains"]
    # Sorted so the card leads with what carries the most information: core
    # series first, then whichever readings sit furthest from the middle.
    shown = sorted(
        voted,
        key=lambda item: (
            item["priority"] != "core",
            # The level card ranks by how far the series travelled this week;
            # a rate sitting at the top of its range is news the first time
            # and arithmetic thereafter.
            -abs((item.get("movement") or {}).get("change") or 0) if not votes
            else -abs((item["risk_percentile"] or 50) - 50),
        ),
    )
    return {
        **{name: spec[name] for name in ("key", "label", "question")},
        "mode": spec["mode"],
        "stance": stance,
        "score": score,
        "reading": reading,
        "split": votes and (
            any(item["vote"] > 0 for item in voted)
            and any(item["vote"] < 0 for item in voted)
        ),
        "evidence": shown[:EVIDENCE_LIMIT],
        "more": max(0, len(shown) - EVIDENCE_LIMIT),
        "pending": [
            {"key": item["key"], "label": item["label"], "reason": item["reason"]}
            for item in evidence if not item["voted"]
        ],
        "confidence": confidence(evidence, len(members)),
    }


def cards(today: date | None = None) -> dict:
    """All five layers, plus what the set of them says as a whole."""
    catalog = indicators.catalog()
    when = today or kst_today()
    built = [layer(spec, catalog, when) for spec in LAYERS]
    # Only the voting layers have a stance to agree or disagree about.
    stances = [
        item["stance"] for item in built
        if item["mode"] == "stance" and item["stance"] != "unknown"
    ]
    return {
        "as_of": when.isoformat(),
        "layers": built,
        # A split across layers is the finding, not a defect to be averaged
        # away. Naming it here keeps the brief from having to re-derive it.
        "split": len(set(stances)) > 1,
        "agreement": (
            f"{len(stances)}개 층 중 "
            f"{max((stances.count(name) for name in set(stances)), default=0)}개가 "
            f"같은 방향입니다" if stances else "판정 가능한 층이 없습니다"
        ),
        # Summed from the layers' own counts, not from their displayed
        # evidence — the cards cap what they show, and counting the cap as
        # the evidence would report a third of the base that actually voted.
        "confidence": _combined([card["confidence"] for card in built]),
        "warning": (
            "층별 판정은 각 계열이 자기 분포에서 어디 있는지를 모은 것입니다. "
            "예측이 아니고, 층이 갈릴 때는 합성보다 갈림이 더 많은 정보를 "
            "담습니다."
        ),
    }


def _combined(parts: list[dict]) -> dict:
    """One confidence over every layer, from their counts rather than their cards."""
    expected = sum(part["expected"] for part in parts)
    fresh = sum(part["fresh"] for part in parts)
    stale = sum(part["stale"] for part in parts)
    strength = (fresh + 0.5 * stale) / expected if expected else 0.0
    return {
        "level": "high" if strength >= 0.7 else "medium" if strength >= 0.4 else "low",
        "strength": round(strength, 3),
        "expected": expected,
        "voted": fresh + stale,
        "fresh": fresh,
        "stale": stale,
        "reasons": [
            reason for part in parts for reason in part["reasons"]
        ],
        "method": parts[0]["method"] if parts else "",
    }


def unmapped() -> list[str]:
    """Analysis groups no layer claims. A test asserts this stays empty."""
    claimed = set().union(*GROUPS_BY_LAYER.values())
    return sorted(
        {spec["analysis_group"] for spec in indicators.catalog().values()} - claimed
    )
