"""What a series is, why it matters, and what its current value means.

The dashboard has two readers and one truth.  An analyst needs the number and
its position; someone meeting the series for the first time needs to know why
anyone watches it.  Building two screens would let them drift, so the same
tile carries both and the difference is how far you open it.

Four of the five layers are written once and reviewed.  The fifth is not
written at all: "what this value means right now" is composed from the
distribution reading at render time, because a sentence with a number in it
is wrong by Monday.  That is also the layer no static glossary can offer —
the system knows where today's value sits, so it can say so.
"""
from __future__ import annotations

from app import normalize
from app.collectors import indicators


# Ordered as they unfold.  Depth 0 (value, date, percentile) is the tile
# itself and lives in the interface, not here.
LAYERS = ("what", "why", "how", "watch", "caveat")

LAYER_LABELS = {
    "what": "무엇인가",
    "why": "왜 보는가",
    "how": "어떻게 읽나",
    "watch": "함께 볼 것",
    "caveat": "주의",
}


def _now_sentence(spec: dict, reading: dict) -> str | None:
    """State where today's observation sits, in words, from live data.

    Never stored: this is the one layer that must be recomputed, and writing
    it into the catalogue would freeze a value that changes every session.
    """
    if not reading.get("available"):
        return None
    value, unit = reading["value"], spec.get("unit") or ""
    place = f"{reading['window_label']} 분포에서 상위 {reading['percentile']:.0f}%"
    sentence = f"현재 {value}{unit} — {place}입니다"
    if reading.get("risk_percentile") is not None:
        # Only series whose direction is declared get a risk reading, and the
        # framing has to match which direction was declared — the same
        # sentence on a series whose rise is supportive says the opposite of
        # what the data means.
        stance = (
            "위험 회피 쪽" if reading["risk_percentile"] <= 20
            else "위험 선호 쪽" if reading["risk_percentile"] >= 80
            else "중립"
        )
        lead = (
            "상승이 위험 신호인 계열이라"
            if reading["direction"] == indicators.RISK
            else "상승이 여건 완화·활동 강화를 뜻하는 계열이라"
        )
        sentence += f". {lead} 지금 위치는 {stance}입니다"
    sentence += f" (관측일 {reading['as_of']})."
    if reading.get("caveat"):
        sentence += f" {reading['caveat']}"
    return sentence


def explain(key: str) -> dict:
    """Everything the interface needs to teach this series, at every depth."""
    catalog = indicators.catalog()
    if key not in catalog:
        return {"key": key, "available": False, "reason": f"알 수 없는 지표: {key}"}
    spec = catalog[key]
    written = indicators.EXPLANATIONS.get(key)
    reading = normalize.position_for(key)
    return {
        "key": key,
        "label": spec["label"],
        "available": True,
        # A category blurb is honest as a stand-in and dishonest as a
        # per-series explanation, so the interface is told which it has.
        "fallback": written is None,
        "summary": indicators.indicator_description(key),
        "layers": (
            {
                name: written[name] for name in LAYERS
                if name != "watch" and written.get(name)
            }
            if written else {}
        ),
        # Kept apart from the prose layers because it is a reference list, not
        # a paragraph: the interface links each key, and the linkage layer
        # will walk it. Unknown keys are a test failure, not a dead link.
        "watch": [
            {
                "key": target,
                "why": why,
                "label": (catalog.get(target) or {}).get("label", target),
            }
            for target, why in ((written or {}).get("watch") or [])
        ],
        "now": _now_sentence(spec, reading),
        "position": reading,
        "source": spec["source"],
        "source_url": spec["source_url"],
        "proxy": spec["proxy"],
        "frequency": indicators.cycle_of(key),
        "date_kind": spec["date_kind"],
    }


GUIDES = (
    {
        "slug": "finance",
        "title": "금융 기초",
        "path": "docs/finance-guide.md",
        "lead": "금리·물가·환율이 서로 무엇을 하는지부터.",
    },
    {
        "slug": "methods",
        "title": "분석 방법과 해석 제한",
        "path": "docs/analysis-methods.md",
        "lead": "이 화면들의 분석이 무엇을 말할 수 있고 무엇을 말할 수 없는지.",
    },
)


def guide(slug: str) -> dict | None:
    """One of the written guides, as Markdown source.

    These have existed in the repository since the beginning and were
    unreachable from the screens holding the numbers they explain. Serving
    them from the same file keeps one copy rather than a web transcription
    that drifts from the repository one.
    """
    from pathlib import Path

    for item in GUIDES:
        if item["slug"] != slug:
            continue
        path = Path(__file__).resolve().parent.parent / item["path"]
        if not path.exists():
            return {**item, "body": "", "missing": True}
        return {**item, "body": path.read_text(encoding="utf-8"), "missing": False}
    return None


def coverage() -> dict:
    """How much of the catalogue teaches rather than labels."""
    catalog = indicators.catalog()
    core = [key for key, spec in catalog.items() if spec["priority"] == "core"]
    written = set(indicators.EXPLANATIONS)
    return {
        "total": len(catalog),
        "written": len(written),
        "core_total": len(core),
        "core_written": len([key for key in core if key in written]),
    }
