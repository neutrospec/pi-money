"""What moved, what disagrees, and what would change the verdict.

Every other screen answers "what is true". This one answers the two questions
that actually precede a decision: what changed since last week, and what would
have to happen for today's reading to read differently.

It never says buy or sell. A watch condition is arithmetic — which component
would have to flip for the verdict to flip, and how far its own distribution
would have to move to get there — not a recommendation.

Discipline that keeps it readable: the whole brief must fit one screen. Movers
are capped, disagreements appear only when they exist, and a section with
nothing in it is omitted rather than shown empty. A brief with four sections
always filled is a brief nobody reads. Adding an item here means removing one.
"""
from __future__ import annotations

from app import analysis, db, market_metrics, normalize, sentiment
from app.collectors import indicators
from app.timeutil import kst_today


BOUNDARY = (
    "매수·매도를 말하지 않습니다. 무엇이 보이는가와, 무엇이 바뀌면 판정이 "
    "달라지는가만 답합니다."
)

MOVER_LIMIT = 8
# Below this a distribution move is noise dressed as news.
MOVER_THRESHOLD = 8.0


def _korea_regime() -> dict:
    return analysis.korea_regime(
        db.get_indicator_points("kr_vkospi"),
        market_metrics.aligned_spread_series("kr_corp_bond_3y", "kr_treasury_3y"),
        market_metrics.aligned_spread_series("kr_cp_91d", "kr_cd_91d"),
        db.get_index_points("^KS11"),
    )


def _us_regime() -> dict:
    return analysis.market_regime(
        db.get_indicator_points("us_vix"),
        db.get_indicator_points("us_ig_spread"),
        db.get_index_points("^GSPC"),
    )


def movers(limit: int = MOVER_LIMIT) -> list[dict]:
    """Series that changed position within their own distribution this week."""
    catalog = indicators.catalog()
    found = []
    for key, spec in catalog.items():
        moved = normalize.movement_for(key)
        if not moved or abs(moved["change"]) < MOVER_THRESHOLD:
            continue
        written = indicators.EXPLANATIONS.get(key) or {}
        found.append({
            "key": key,
            "label": spec["label"],
            "unit": spec["unit"],
            "direction": indicators.risk_direction(key),
            **moved,
            # The explanation's related keys are what to read next, already
            # written as references rather than prose.
            "watch": [
                {"key": target, "why": why,
                 "label": (catalog.get(target) or {}).get("label", target)}
                for target, why in written.get("watch", [])
            ],
        })
    found.sort(key=lambda item: -abs(item["change"]))
    return found[:limit]


def _flip_conditions(regime: dict) -> list[dict]:
    """Which single component would have to change for the verdict to change.

    Pure arithmetic on the votes already cast: the classifier's verdict is a
    ratio of the net vote to the components that reported, so substituting one
    vote and re-reading the rule says exactly what it would take. No model,
    no forecast — the same rule, asked a different question.
    """
    active = regime.get("components") or []
    if len(active) < analysis.KR_MIN_ACTIVE_COMPONENTS:
        return []

    def verdict(total: int) -> str:
        ratio = total / len(active)
        if ratio >= 0.5:
            return "risk_on"
        if ratio <= -0.5:
            return "risk_off"
        return "neutral"

    current = verdict(regime["score"])
    out = []
    for component in active:
        for candidate in (1, 0, -1):
            if candidate == component["score"]:
                continue
            moved = verdict(regime["score"] - component["score"] + candidate)
            if moved == current:
                continue
            gap = None
            percentile = component.get("percentile")
            if percentile is not None:
                target = (
                    analysis.KR_RISK_ON_PERCENTILE if candidate > 0
                    else analysis.KR_RISK_OFF_PERCENTILE
                )
                gap = round(target - percentile, 1)
            out.append({
                "key": component["key"],
                "label": component["label"],
                "from_score": component["score"],
                "to_score": candidate,
                "verdict": moved,
                "percentile": percentile,
                "percentile_gap": gap,
                "detail": component.get("detail"),
            })
            break   # the nearest flip for this component is the informative one
    # Saying which verdicts no single component can reach is as much of an
    # answer as listing the ones it can. Silence there reads as "nothing to
    # report" when the truth is "one input is not enough".
    reachable = {item["verdict"] for item in out} | {current}
    for verdict_name in ("risk_on", "risk_off", "neutral"):
        if verdict_name not in reachable:
            out.append({
                "key": None,
                "label": None,
                "verdict": verdict_name,
                "unreachable": True,
                "detail": f"구성요소 하나만 바뀌어서는 {verdict_name}에 "
                          f"도달하지 않습니다. 현재 순점수 "
                          f"{regime['score']:+d}/{len(active)}에서 최소 두 층이 "
                          f"함께 움직여야 합니다.",
            })
    return out


def disagreements(korea: dict, us: dict, gauge: dict) -> list[dict]:
    """Only the contradictions that exist today. Absent kinds are omitted."""
    out = []
    if korea["regime"] != us["regime"]:
        out.append({
            "kind": "regime_split",
            "title": "한국과 미국 국면이 갈립니다",
            "detail": f"한국 {korea['regime']} (순점수 {korea['score']:+d}) · "
                      f"미국 {us['regime']} (순점수 {us['score']:+d})",
            "why": "두 분류기는 서로 다른 시장의 입력을 씁니다. 미국 판정을 "
                   "한국 자산의 근거로 그대로 옮기지 마세요.",
            "evidence": [item["detail"] for item in korea.get("components", [])]
                        + list(us.get("reasons", [])),
        })
    votes = [item["score"] for item in korea.get("components", [])]
    if any(vote > 0 for vote in votes) and any(vote < 0 for vote in votes):
        out.append({
            "kind": "component_split",
            "title": "한국 국면 구성요소가 서로 반대로 투표합니다",
            "detail": f"찬성 {sum(1 for v in votes if v > 0)}개 · "
                      f"반대 {sum(1 for v in votes if v < 0)}개 · "
                      f"중립 {sum(1 for v in votes if v == 0)}개",
            "why": "합성 판정보다 어느 층이 갈리는지가 더 많은 정보를 담습니다.",
            "evidence": [
                f"{item['label']}: {item['detail']}"
                for item in korea.get("components", []) if item["score"]
            ],
        })
    parts = [item for item in gauge.get("components", []) if item.get("score") is not None]
    if parts:
        low = min(parts, key=lambda item: item["score"])
        high = max(parts, key=lambda item: item["score"])
        if high["score"] - low["score"] >= 60:
            out.append({
                "kind": "sentiment_spread",
                "title": "시장 심리 구성요소가 크게 갈립니다",
                "detail": f"{low['label']} {low['score']} ↔ "
                          f"{high['label']} {high['score']}",
                "why": "합성 점수는 이 격차를 평균으로 지웁니다. "
                       "게이지의 실질적 가치는 합성이 아니라 불일치입니다.",
                "evidence": [f"{low['label']}: {low['detail']}",
                             f"{high['label']}: {high['detail']}"],
            })
    return out


def unresolved(korea: dict, gauge: dict) -> list[dict]:
    """Evidence that did not vote, and why. Never silently treated as neutral."""
    out = [
        {"source": "한국 국면", "label": item.get("label", item["key"]),
         "reason": item["reason"]}
        for item in korea.get("pending", [])
    ]
    out += [
        {"source": "시장 심리", "label": item.get("key"), "reason": item["reason"]}
        for item in gauge.get("pending", [])
    ]
    return out


def brief() -> dict:
    korea, us, gauge = _korea_regime(), _us_regime(), sentiment.gauge()
    return {
        "as_of": kst_today().isoformat(),
        "korea_regime": korea,
        "regime": us,
        "sentiment": gauge,
        "disagreements": disagreements(korea, us, gauge),
        "flip_conditions": _flip_conditions(korea),
        "movers": movers(),
        "unresolved": unresolved(korea, gauge),
        "lookback_days": normalize.LOOKBACK_DAYS,
        "warning": BOUNDARY,
        "cached": True,
    }
