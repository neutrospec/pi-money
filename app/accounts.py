"""Account types, statutory limits, and what each account may not hold.

Korean tax-advantaged accounts differ in ways a portfolio model has to know:
a pension account cannot hold individual stocks, an ISA has both an annual and
a lifetime contribution ceiling, and a DC plan caps risky assets at 70%. Those
rules decide what a holding *means*, so they live in one declared place rather
than being remembered at each call site.

Every policy item carries a ``status``, and that field is the reason this
module exists rather than a handful of constants. Widely-circulated Korean
personal-finance articles quote an ISA limit of 40 million won a year with 5
million tax-free; that is the 2024 amendment bill, which **did not pass the
National Assembly**. The figures in force are 20 million and 2 million (4
million for the 서민형 tier). A number that is merely proposed must never be
rendered where a current one belongs, and must never enter a calculation.

``scope`` is the second field that cannot be dropped. The pension contribution
ceiling is per *person* across 연금저축 and IRP combined, while the ISA limits
are per *account*. Treating a per-person limit as per-account shows twice the
headroom that exists.

Nothing here computes tax, and nothing here says what to buy. The buy gates
exist so that a holding which the account legally cannot contain is reported as
a data-entry question, not as advice.
"""
from __future__ import annotations


# The five account types the owner holds, plus room for what the government has
# already drafted. No SQLite CHECK constraint enforces this: a CHECK cannot be
# altered without rebuilding the table, and a sixth type (생산적금융 ISA) is
# already in a government bill. Validation lives here instead.
ACCOUNT_TYPES = {
    "general": "일반 종합위탁",
    "pension_savings": "개인연금저축",
    "retirement_dc": "퇴직연금 DC",
    "isa": "ISA (중개형)",
    "managed": "위탁 (외부 관리)",
}

# What each account may NOT hold, expressed as the difference from a general
# account. Stating the differences keeps the list short enough to check against
# a regulation, where an allowlist per type would drift silently.
BUY_GATES = {
    "general": (),
    "managed": (),
    "pension_savings": (
        "individual_stock", "leveraged_etf", "inverse_etf", "bond", "derivative",
    ),
    "retirement_dc": ("individual_stock", "leveraged_etf", "inverse_etf"),
    # Domestic individual stocks are allowed; foreign ones are not.
    "isa": ("foreign_individual_stock",),
}

GATE_LABELS = {
    "individual_stock": "개별주식",
    "foreign_individual_stock": "해외 개별주식",
    "leveraged_etf": "레버리지 ETF",
    "inverse_etf": "인버스 ETF",
    "bond": "채권",
    "derivative": "선물·옵션",
}

# Every policy item carries these eight fields. A test asserts it, because a
# missing ``status`` or ``scope`` is exactly the kind of omission that turns a
# proposed figure into a displayed one or doubles a per-person ceiling.
POLICY_FIELDS = (
    "value", "unit", "scope", "status", "effective_from", "confirmed_on",
    "source_url", "note",
)

IN_FORCE = "in_force"
PROPOSED = "proposed"        # drafted, not passed. Never rendered as current.
UNVERIFIED = "unverified"

POLICY = {
    "pension_contribution_annual": {
        "value": 18_000_000, "unit": "KRW", "scope": "user", "status": IN_FORCE,
        "effective_from": "2023-01-01", "confirmed_on": "2026-08-29",
        "source_url": "https://www.nts.go.kr/",
        "note": "연금저축과 IRP를 합산한 1인당 한도입니다. 계좌별이 아닙니다.",
    },
    "pension_deduction_annual": {
        "value": 9_000_000, "unit": "KRW", "scope": "user", "status": IN_FORCE,
        "effective_from": "2023-01-01", "confirmed_on": "2026-08-29",
        "source_url": "https://www.nts.go.kr/",
        "note": "세액공제 대상 한도(연금저축 단독은 600만원). 납입 한도와 다릅니다.",
    },
    "isa_contribution_annual": {
        "value": 20_000_000, "unit": "KRW", "scope": "account", "status": IN_FORCE,
        "effective_from": "2021-01-01", "confirmed_on": "2026-08-29",
        "source_url": "https://www.fsc.go.kr/",
        "note": "현행입니다. 널리 인용되는 4,000만원은 국회를 통과하지 못한 "
                "2024년 개정안입니다.",
    },
    "isa_contribution_lifetime": {
        "value": 100_000_000, "unit": "KRW", "scope": "account", "status": IN_FORCE,
        "effective_from": "2021-01-01", "confirmed_on": "2026-08-29",
        "source_url": "https://www.fsc.go.kr/",
        "note": "총 납입 한도.",
    },
    "isa_tax_free_general": {
        "value": 2_000_000, "unit": "KRW", "scope": "account", "status": IN_FORCE,
        "effective_from": "2021-01-01", "confirmed_on": "2026-08-29",
        "source_url": "https://www.fsc.go.kr/",
        "note": "일반형 비과세 한도. 서민형은 400만원입니다.",
    },
    "isa_contribution_annual_proposed": {
        "value": 40_000_000, "unit": "KRW", "scope": "account", "status": PROPOSED,
        "effective_from": None, "confirmed_on": "2026-08-29",
        "source_url": "https://www.fsc.go.kr/",
        "note": "정부안이며 국회를 통과하지 않았습니다. 현행이 아닙니다.",
    },
    "dc_risky_asset_limit": {
        "value": 70, "unit": "%", "scope": "account", "status": IN_FORCE,
        "effective_from": "2015-07-01", "confirmed_on": "2026-08-29",
        "source_url": "https://www.moel.go.kr/",
        "note": "위험자산 기준은 '주식 비중 40% 초과 펀드·ETF'입니다. ETF 구성 "
                "원천이 없어 자동 판정이 불가능하므로 미분류는 미상으로 둡니다.",
    },
    "pension_withdrawal_age": {
        "value": 55, "unit": "세", "scope": "user", "status": IN_FORCE,
        "effective_from": "2013-03-01", "confirmed_on": "2026-08-29",
        "source_url": "https://www.nts.go.kr/",
        "note": "가입 후 5년 경과 요건은 세법상 가입일(tax_opened_on) 기준입니다.",
    },
}

# Exposure axes. Deliberately coarse: these are what a holding is evidence of,
# and they have to line up with indicator analysis groups without pretending to
# a precision that owner-confirmed tagging cannot deliver.
EXPOSURE_TAGS = {
    "kr_equity": "국내 주식",
    "us_equity": "미국 주식",
    "global_equity": "기타 해외 주식",
    "kr_bond": "국내 채권",
    "global_bond": "해외 채권",
    "credit": "크레딧",
    "commodity": "원자재",
    "real_asset": "부동산·인프라",
    "cash": "현금성",
    "unclassified": "미분류",
}

CASHFLOW_KINDS = {
    "deposit": "입금",
    "withdrawal": "출금",
    # Kept apart from deposit: an ISA maturity rolled into a pension account is
    # excluded from the contribution ceiling, so merging the two would overstate
    # how much room is left.
    "transfer_in": "이전 입금",
    "transfer_out": "이전 출금",
}


def in_force(key: str) -> dict | None:
    """A policy item only if it is actually in force, else None.

    The guard is the point. Every caller that wants a number to display or to
    compare against gets None for a proposed one, so a bill that has not passed
    cannot reach a screen through an unguarded lookup.
    """
    item = POLICY.get(key)
    return item if item and item["status"] == IN_FORCE else None


def gates_for(account_type: str) -> tuple[str, ...]:
    return BUY_GATES.get(account_type, ())


def label_for(account_type: str) -> str:
    return ACCOUNT_TYPES.get(account_type, account_type)


def policy_for(account_type: str) -> list[dict]:
    """The policy items that apply to this account type, current ones first."""
    applies = {
        "pension_savings": ("pension_contribution_annual",
                            "pension_deduction_annual", "pension_withdrawal_age"),
        "retirement_dc": ("dc_risky_asset_limit", "pension_withdrawal_age"),
        "isa": ("isa_contribution_annual", "isa_contribution_lifetime",
                "isa_tax_free_general", "isa_contribution_annual_proposed"),
        "general": (), "managed": (),
    }
    return [
        {"key": key, **POLICY[key]}
        for key in applies.get(account_type, ())
        if key in POLICY
    ]
