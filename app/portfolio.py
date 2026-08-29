"""What the owner holds, read beside what the indicators say.

Three things shape every decision in here, and all three come from measurement
rather than preference.

**Past value cannot be reconstructed.** Domestic instrument prices go back
about 23 sessions and foreign ones have no dated history at all. A transaction
ledger would therefore cost daily input and produce nothing computable — no
cost basis, no period return, no benchmark comparison. So this is snapshots,
and the words "수익률" and "취득단가" do not appear as computed quantities.

**There is no single total.** A portfolio has holdings priced from a live
market, holdings priced from a session that has gone stale, holdings the owner
had to state a value for because nothing can price them, and holdings with no
value at all. Adding those four produces a number that means nothing, and
adding across currencies would need an FX rate whose observation date differs
from both sides. Valuation is therefore always a table keyed by grade and
currency, and there is a regression test asserting no scalar total exists.

**Unknown is not zero.** ``is_risky_asset`` is NULL when nobody has classified
the holding, and the DC 70% limit is reported as undecidable while any holding
in that account is NULL. Counting unknowns as safe would misstate the one
constraint that account actually has.

This module reads and writes through a CLI. There is no web write path and no
agent write tool: the repository is public, and an asset picture that reaches
an external model cannot be recalled.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta

from app import accounts, db
from app.timeutil import kst_today


# How a holding's value was arrived at. Kept apart everywhere, never summed.
MARKET = "market"            # a market close from within the freshness window
STALE = "stale"              # a market close, but older than the window
USER_STATED = "user_stated"  # the owner supplied it; nothing can price this
UNPRICED = "unpriced"        # no price and no stated value. Never counted as 0.

GRADE_LABELS = {
    MARKET: "시장가", STALE: "지연 시장가",
    USER_STATED: "사용자 입력", UNPRICED: "평가 불가",
}

# A close older than this is graded stale rather than market. Same allowance
# the indicator layer uses for a daily series, so the project has one notion of
# "recent enough" rather than two that can drift apart.
PRICE_MAX_AGE_DAYS = 5

# Bulk tables whose rows carry no close at all — instrument master records, not
# trading records. Joining a holding to one of these would look like a price
# lookup and silently return NULL for every row.
UNPRICED_DATASETS = (
    "stk_isu_base_info", "ksq_isu_base_info", "knx_isu_base_info",
    "sri_bond_info",
)

# Where a plain holding of a listed code lives. Ordered, and the order is a
# declaration: a line that says "005930" means the share, not a future or an
# option written on it. Derivatives are deliberately absent — holding one is
# specific enough that the dataset should be stated rather than guessed.
SPOT_DATASETS = (
    "stk_bydd_trd",     # KOSPI shares
    "ksq_bydd_trd",     # KOSDAQ shares
    "knx_bydd_trd",     # KONEX shares
    "etf_bydd_trd",
    "etn_bydd_trd",
    "elw_bydd_trd",
)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def account_list() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    return [
        {**dict(row),
         "type_label": accounts.label_for(row["account_type"]),
         "gates": [accounts.GATE_LABELS.get(gate, gate)
                   for gate in accounts.gates_for(row["account_type"])]}
        for row in rows
    ]


def latest_as_of(account_id: int) -> str | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(as_of) AS as_of FROM holding_snapshots WHERE account_id=?",
            (account_id,),
        ).fetchone()
    return row["as_of"] if row else None


def resolve_dataset(source: str, symbol: str, dataset: str = "") -> dict:
    """Find which table actually prices this code.

    The design requires a holding to be stored as ``(source, dataset, symbol)``
    because one code can appear in several tables. That is right for storage
    and wrong as a thing to ask a person to type: nobody should have to know
    that 005930 is in ``stk_bydd_trd`` while 102110 is in ``etf_bydd_trd``, and
    getting it wrong produces a holding that silently reports as unvaluable
    when the price is sitting in the cache.

    So the triple is still what gets stored — it is just resolved rather than
    demanded. A stated dataset that prices the code is kept as given. One that
    does not is corrected, and the correction is reported rather than applied
    quietly. A code in more than one spot table is left alone and reported as
    ambiguous, because picking one would be a guess.
    """
    if not source or not symbol:
        return {"dataset": dataset, "corrected": False, "reason": None}
    with db.get_conn() as conn:
        if dataset and dataset not in UNPRICED_DATASETS:
            hit = conn.execute(
                """SELECT 1 FROM market_daily WHERE source=? AND dataset=?
                   AND symbol=? AND close IS NOT NULL LIMIT 1""",
                (source, dataset, symbol),
            ).fetchone()
            if hit:
                return {"dataset": dataset, "corrected": False, "reason": None}
        found = [
            row["dataset"] for row in conn.execute(
                """SELECT DISTINCT dataset FROM market_daily
                   WHERE source=? AND symbol=? AND close IS NOT NULL""",
                (source, symbol),
            ) if row["dataset"] in SPOT_DATASETS
        ]
    if len(found) == 1:
        return {
            "dataset": found[0], "corrected": found[0] != dataset,
            "reason": (f"{dataset or '(빈칸)'} → {found[0]} 로 정정했습니다"
                       if found[0] != dataset else None),
        }
    if len(found) > 1:
        return {"dataset": dataset, "corrected": False,
                "reason": f"{symbol} 이 여러 표에 있습니다 ({', '.join(sorted(found))}) "
                          f"— 어느 것인지 직접 적어주세요"}
    return {"dataset": dataset, "corrected": False,
            "reason": f"{symbol} 의 가격을 캐시에서 찾지 못했습니다"}


def _price(source: str, dataset: str, symbol: str) -> dict | None:
    """The latest cached close for one instrument, or None.

    Instrument-master datasets are excluded by name rather than by discovering
    a NULL close, because a NULL from a master table and a NULL from a genuinely
    unpriced trading row mean different things and only the second is news.
    """
    if not source or not dataset or dataset in UNPRICED_DATASETS:
        return None
    with db.get_conn() as conn:
        row = conn.execute(
            """SELECT date, close FROM market_daily
               WHERE source=? AND dataset=? AND symbol=? AND close IS NOT NULL
               ORDER BY date DESC LIMIT 1""",
            (source, dataset, symbol),
        ).fetchone()
    return dict(row) if row else None


def _grade(holding: dict, today: date) -> dict:
    """Value one holding and say how the value was arrived at."""
    price = _price(holding["source"], holding["dataset"], holding["symbol"])
    if price and holding.get("quantity") is not None:
        age = (today - date.fromisoformat(price["date"])).days
        return {
            "grade": MARKET if age <= PRICE_MAX_AGE_DAYS else STALE,
            "amount": round(price["close"] * holding["quantity"], 2),
            "price": price["close"], "price_date": price["date"],
            "price_age_days": age,
        }
    if holding.get("stated_value") is not None:
        return {"grade": USER_STATED, "amount": holding["stated_value"],
                "price": None, "price_date": None, "price_age_days": None}
    # Not zero. A holding nobody can price is a hole in the picture and has to
    # look like one.
    return {"grade": UNPRICED, "amount": None,
            "price": None, "price_date": None, "price_age_days": None}


def holdings(account_id: int, as_of: str | None = None,
             today: date | None = None) -> list[dict]:
    when = today or kst_today()
    as_of = as_of or latest_as_of(account_id)
    if not as_of:
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM holding_snapshots
               WHERE account_id=? AND as_of=? ORDER BY name""",
            (account_id, as_of),
        ).fetchall()
    return [{**dict(row), **_grade(dict(row), when)} for row in rows]


def valuation(items: list[dict]) -> dict:
    """Amounts by (grade, currency). Deliberately not a total.

    Returning a scalar here would let every caller add a live price to a stale
    one to a number the owner typed, across currencies, and print it as net
    worth. The shape is the guarantee.
    """
    buckets: dict[tuple[str, str], dict] = {}
    for item in items:
        key = (item["grade"], item["currency"])
        cell = buckets.setdefault(key, {"grade": item["grade"],
                                        "currency": item["currency"],
                                        "amount": 0.0, "holdings": 0})
        cell["holdings"] += 1
        if item["amount"] is not None:
            cell["amount"] = round(cell["amount"] + item["amount"], 2)
    return {
        "buckets": sorted(buckets.values(),
                          key=lambda cell: (cell["currency"], cell["grade"])),
        "unpriced": sum(1 for item in items if item["grade"] == UNPRICED),
        "note": (
            "등급과 통화별로만 냅니다. 시장가·지연가·사용자 입력·평가 불가를 "
            "더하면 뜻이 없는 숫자가 되고, 통화를 넘어 더하려면 관측일이 서로 "
            "다른 환율이 필요합니다."
        ),
    }


def risky_share(account: dict, items: list[dict]) -> dict:
    """The DC risky-asset share, or a refusal to state one.

    While any holding in the account is unclassified the share is not reported.
    Counting unknowns as safe, or dropping them from the denominator, would
    both report a number the data does not support — and this is the one
    constraint a DC account actually has.
    """
    limit = accounts.in_force("dc_risky_asset_limit")
    if account["account_type"] != "retirement_dc" or not items:
        return {"applicable": False}
    valued = [item for item in items if item["amount"] is not None]
    unknown = [item for item in valued if item["is_risky_asset"] is None]
    unpriced = [item for item in items if item["amount"] is None]
    base = {
        "applicable": True,
        "limit_pct": limit["value"] if limit else None,
        "limit_note": limit["note"] if limit else None,
        "unclassified": len(unknown),
        "unpriced": len(unpriced),
    }
    if unknown or unpriced:
        return {**base, "decidable": False,
                "reason": f"판정 불가 — 미분류 {len(unknown)}건"
                          + (f", 평가 불가 {len(unpriced)}건" if unpriced else "")}
    total = sum(item["amount"] for item in valued)
    risky = sum(item["amount"] for item in valued if item["is_risky_asset"])
    return {**base, "decidable": True,
            "share_pct": round(risky / total * 100, 1) if total else None}


def gate_conflicts(account: dict, items: list[dict]) -> list[dict]:
    """Holdings the account legally cannot contain.

    Reported as a data-entry question, never as a trade. The gates say what may
    not be bought; a holding that contradicts one is far more likely to be a
    mistyped account than a regulatory breach.
    """
    gates = set(accounts.gates_for(account["account_type"]))
    if not gates:
        return []
    return [
        {"symbol": item["symbol"], "name": item["name"], "tagged": item["note"],
         "gate": accounts.GATE_LABELS.get(gate, gate),
         "detail": f"{accounts.label_for(account['account_type'])}에서 "
                   f"{accounts.GATE_LABELS.get(gate, gate)}는 매수할 수 없는 "
                   f"상품입니다 — 입력을 확인하세요"}
        for item in items
        for gate in gates
        if (item["note"] or "") .strip() == gate
    ]


def overview(today: date | None = None) -> dict:
    """Every account with its holdings, valuation grades and constraints."""
    when = today or kst_today()
    built = []
    for account in account_list():
        as_of = latest_as_of(account["id"])
        items = holdings(account["id"], as_of, when)
        built.append({
            **account,
            "as_of": as_of,
            "stale_days": (
                (when - date.fromisoformat(as_of)).days if as_of else None
            ),
            "holdings": items,
            "valuation": valuation(items),
            "risky": risky_share(account, items),
            "conflicts": gate_conflicts(account, items),
            "policy": accounts.policy_for(account["account_type"]),
        })
    return {
        "as_of": when.isoformat(),
        "accounts": built,
        "empty": not built,
        "grade_labels": GRADE_LABELS,
        "warning": (
            "총자산 단일 숫자를 만들지 않습니다. 평가 등급과 통화별로만 "
            "보여주며, 환산하지 않습니다. 매수·매도를 말하지 않습니다."
        ),
    }


# ---------------------------------------------------------------------------
# Writes — CLI only. No web endpoint, no agent tool.
# ---------------------------------------------------------------------------
def add_account(label: str, institution: str, account_type: str, *,
                opened_on: str | None = None, tax_opened_on: str | None = None,
                currency: str = "KRW", note: str | None = None) -> int:
    if account_type not in accounts.ACCOUNT_TYPES:
        raise ValueError(
            f"알 수 없는 계좌 유형: {account_type} "
            f"(가능: {', '.join(accounts.ACCOUNT_TYPES)})"
        )
    with db.get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO accounts
               (label, institution, account_type, opened_on, tax_opened_on,
                currency, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (label, institution, account_type, opened_on,
             tax_opened_on or opened_on, currency, note, db.utc_now()),
        )
        return cursor.lastrowid


def update_account(account_id: int, **changes) -> dict:
    """Correct an account's details. Everything except its identity as a row.

    Needed because the type is not cosmetic: it decides which instruments the
    account may hold and which contribution limits apply. The first version of
    the web form let the type select default silently to the first option, so
    a pension account could be recorded as a general one — and without this,
    the only fix was editing SQLite by hand.
    """
    fields = ("label", "institution", "account_type", "opened_on",
              "tax_opened_on", "currency", "note")
    updates = {key: value for key, value in changes.items()
               if key in fields and value not in (None, "")}
    if not updates:
        raise ValueError("바꿀 항목이 없습니다")
    if "account_type" in updates and updates["account_type"] not in accounts.ACCOUNT_TYPES:
        raise ValueError(
            f"알 수 없는 계좌 유형: {updates['account_type']} "
            f"(가능: {', '.join(accounts.ACCOUNT_TYPES)})"
        )
    assignments = ", ".join(f"{key}=:{key}" for key in updates)
    with db.get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE accounts SET {assignments} WHERE id=:id",
            {**updates, "id": account_id},
        )
        if not cursor.rowcount:
            raise ValueError(f"계좌 {account_id}를 찾을 수 없습니다")
    return {"id": account_id, "changed": sorted(updates)}


def _parse_rows(text: str) -> list[dict]:
    """CSV text to normalised dicts. One parser for the CLI and the browser.

    Splitting these would let the two entry paths disagree about what an empty
    ``is_risky_asset`` means, and that difference is exactly the one the DC
    limit depends on.
    """
    return [
        {key.strip(): (value or "").strip()
         for key, value in row.items() if key}
        for row in csv.DictReader(text.lstrip("\ufeff").splitlines())
    ]


def _read_rows(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return _parse_rows(handle.read())


def _flag(value: str) -> int | None:
    """Three-valued: an absent classification stays unknown, never False."""
    text = (value or "").strip().lower()
    if text in ("1", "y", "yes", "true", "위험"):
        return 1
    if text in ("0", "n", "no", "false", "안전"):
        return 0
    return None


def import_snapshot(account_id: int, as_of: str, path: str, *,
                    dry_run: bool = False) -> dict:
    """Replace this account's snapshot for ``as_of`` from a CSV file."""
    return import_rows(account_id, as_of, _read_text(path), dry_run=dry_run)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8-sig") as handle:
        return handle.read()


def import_rows(account_id: int, as_of: str, text: str, *,
                dry_run: bool = False) -> dict:
    """Replace this account's snapshot for ``as_of`` entirely.

    Full replacement rather than merge, because a partial import is how a sold
    position stays on the books forever. Destructive by design, so ``dry_run``
    reports what would go and what would arrive before anything is written —
    the browser surfaces that as a preview step rather than an option.
    """
    date.fromisoformat(as_of)   # reject a malformed date here, not in SQL
    rows = _parse_rows(text)
    if not rows:
        raise ValueError("적재할 행이 없습니다 — 머리글과 최소 한 줄이 필요합니다")
    parsed, resolutions = [], []
    for row in rows:
        source = row.get("source") or ""
        symbol = row.get("symbol") or ""
        found = resolve_dataset(source, symbol, row.get("dataset") or "")
        if found["reason"]:
            resolutions.append({"symbol": symbol, "name": row.get("name") or symbol,
                                **found})
        parsed.append({
            "account_id": account_id, "as_of": as_of,
            "source": source,
            "dataset": found["dataset"],
            "symbol": symbol,
            "name": row.get("name") or symbol,
            "quantity": float(row["quantity"]) if row.get("quantity") else None,
            "book_amount": (
                float(row["book_amount"]) if row.get("book_amount") else None
            ),
            "stated_value": (
                float(row["stated_value"]) if row.get("stated_value") else None
            ),
            "currency": row.get("currency") or "KRW",
            "is_risky_asset": _flag(row.get("is_risky_asset", "")),
            "note": row.get("note") or None,
            "recorded_at": db.utc_now(),
        })
    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM holding_snapshots "
            "WHERE account_id=? AND as_of=?", (account_id, as_of),
        ).fetchone()["n"]
        if dry_run:
            return {"dry_run": True, "account_id": account_id, "as_of": as_of,
                    "would_remove": existing, "would_add": len(parsed),
                    "resolutions": resolutions, "rows": parsed}
        conn.execute(
            "DELETE FROM holding_snapshots WHERE account_id=? AND as_of=?",
            (account_id, as_of),
        )
        conn.executemany(
            """INSERT INTO holding_snapshots
               (account_id, as_of, source, dataset, symbol, name, quantity,
                book_amount, stated_value, currency, is_risky_asset, note,
                recorded_at)
               VALUES (:account_id, :as_of, :source, :dataset, :symbol, :name,
                       :quantity, :book_amount, :stated_value, :currency,
                       :is_risky_asset, :note, :recorded_at)""",
            parsed,
        )
    return {"dry_run": False, "account_id": account_id, "as_of": as_of,
            "removed": existing, "added": len(parsed),
            "resolutions": resolutions}


def repair_datasets(account_id: int | None = None, *, dry_run: bool = False) -> dict:
    """Re-resolve stored holdings whose dataset does not price them.

    Rows written before the import learned to resolve are still wrong, and a
    wrong dataset is invisible on screen — the holding just reads as
    unvaluable. This corrects them explicitly and reports each change rather
    than fixing things behind the reader's back.
    """
    where = " WHERE account_id=?" if account_id else ""
    params = (account_id,) if account_id else ()
    with db.get_conn() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT account_id, as_of, source, dataset, symbol, name "
            "FROM holding_snapshots" + where, params)]
    changes = []
    for row in rows:
        found = resolve_dataset(row["source"], row["symbol"], row["dataset"])
        if not found["corrected"]:
            continue
        changes.append({**{key: row[key] for key in
                           ("account_id", "as_of", "symbol", "name")},
                        "from": row["dataset"], "to": found["dataset"]})
        if dry_run:
            continue
        with db.get_conn() as conn:
            conn.execute(
                """UPDATE holding_snapshots SET dataset=?
                   WHERE account_id=? AND as_of=? AND source=? AND dataset=?
                     AND symbol=?""",
                (found["dataset"], row["account_id"], row["as_of"],
                 row["source"], row["dataset"], row["symbol"]),
            )
    return {"dry_run": dry_run, "examined": len(rows), "changed": len(changes),
            "changes": changes}


def record_flow(account_id: int, day: str, kind: str, amount: float, *,
                currency: str = "KRW", note: str | None = None) -> int:
    if kind not in accounts.CASHFLOW_KINDS:
        raise ValueError(
            f"알 수 없는 현금흐름 종류: {kind} "
            f"(가능: {', '.join(accounts.CASHFLOW_KINDS)})"
        )
    with db.get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO account_cashflows
               (account_id, date, kind, amount, currency, note, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (account_id, day, kind, amount, currency, note, db.utc_now()),
        )
        return cursor.lastrowid


def tag_instrument(source: str, dataset: str, symbol: str, tag: str, *,
                   weight: float = 1.0, note: str | None = None) -> None:
    if tag not in accounts.EXPOSURE_TAGS:
        raise ValueError(
            f"알 수 없는 노출 태그: {tag} "
            f"(가능: {', '.join(accounts.EXPOSURE_TAGS)})"
        )
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO instrument_exposure
               (source, dataset, symbol, tag, weight, confirmed_on, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, dataset, symbol, tag) DO UPDATE SET
                 weight=excluded.weight, confirmed_on=excluded.confirmed_on,
                 note=excluded.note""",
            (source, dataset, symbol, tag, weight, kst_today().isoformat(), note),
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="계좌·보유 자산 관리 (읽기는 웹, 쓰기는 여기서만)")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add-account", help="계좌 추가 (계좌번호는 저장하지 않습니다)")
    add.add_argument("--label", required=True, help="식별용 이름")
    add.add_argument("--institution", required=True, help="기관명")
    add.add_argument("--type", required=True, choices=sorted(accounts.ACCOUNT_TYPES))
    add.add_argument("--opened-on", help="개설일 YYYY-MM-DD")
    add.add_argument("--tax-opened-on", help="세법상 가입일 (계약이전 시 승계)")
    add.add_argument("--currency", default="KRW")
    add.add_argument("--note")

    imp = sub.add_parser("import", help="보유 스냅샷 CSV 적재 (해당 날짜를 전량 교체)")
    imp.add_argument("--account", type=int, required=True)
    imp.add_argument("--as-of", required=True, help="기준일 YYYY-MM-DD")
    imp.add_argument("--file", required=True,
                     help="열: source,dataset,symbol,name,quantity,book_amount,"
                          "stated_value,currency,is_risky_asset,note")
    imp.add_argument("--dry-run", action="store_true")

    flow = sub.add_parser("flow", help="입출금 기록")
    flow.add_argument("--account", type=int, required=True)
    flow.add_argument("--date", required=True)
    flow.add_argument("--kind", required=True, choices=sorted(accounts.CASHFLOW_KINDS))
    flow.add_argument("--amount", type=float, required=True)
    flow.add_argument("--currency", default="KRW")
    flow.add_argument("--note")

    tag = sub.add_parser("tag", help="종목에 노출 태그 부여")
    tag.add_argument("--source", required=True)
    tag.add_argument("--dataset", required=True)
    tag.add_argument("--symbol", required=True)
    tag.add_argument("--tag", required=True, choices=sorted(accounts.EXPOSURE_TAGS))
    tag.add_argument("--weight", type=float, default=1.0)
    tag.add_argument("--note")

    edit = sub.add_parser("edit-account", help="계좌 정보 수정 (유형 오입력 정정 등)")
    edit.add_argument("--account", type=int, required=True)
    edit.add_argument("--label")
    edit.add_argument("--institution")
    edit.add_argument("--type", choices=sorted(accounts.ACCOUNT_TYPES))
    edit.add_argument("--opened-on")
    edit.add_argument("--tax-opened-on")
    edit.add_argument("--currency")
    edit.add_argument("--note")

    sub.add_parser("list", help="계좌 목록")

    args = parser.parse_args()
    db.init_db()
    if args.command == "add-account":
        new_id = add_account(
            args.label, args.institution, args.type,
            opened_on=args.opened_on, tax_opened_on=args.tax_opened_on,
            currency=args.currency, note=args.note)
        print(f"계좌 {new_id} 추가: {args.label} ({accounts.label_for(args.type)})")
    elif args.command == "import":
        result = import_snapshot(args.account, args.as_of, args.file,
                                 dry_run=args.dry_run)
        if result["dry_run"]:
            print(f"[미실행] 계좌 {result['account_id']} {result['as_of']}: "
                  f"기존 {result['would_remove']}행 삭제 → "
                  f"{result['would_add']}행 적재")
            for row in result["rows"]:
                print(f"   + {row['symbol']:12s} {row['name'][:20]:22s} "
                      f"수량 {row['quantity']} 위험자산 {row['is_risky_asset']}")
        else:
            print(f"계좌 {result['account_id']} {result['as_of']}: "
                  f"{result['removed']}행 교체 → {result['added']}행")
    elif args.command == "flow":
        record_flow(args.account, args.date, args.kind, args.amount,
                    currency=args.currency, note=args.note)
        print(f"기록: {args.date} {accounts.CASHFLOW_KINDS[args.kind]}")
    elif args.command == "tag":
        tag_instrument(args.source, args.dataset, args.symbol, args.tag,
                       weight=args.weight, note=args.note)
        print(f"태그: {args.symbol} → {accounts.EXPOSURE_TAGS[args.tag]}")
    elif args.command == "edit-account":
        result = update_account(
            args.account, label=args.label, institution=args.institution,
            account_type=args.type, opened_on=args.opened_on,
            tax_opened_on=args.tax_opened_on, currency=args.currency,
            note=args.note)
        print(f"계좌 {result['id']} 수정: {', '.join(result['changed'])}")
    elif args.command == "list":
        found = account_list()
        if not found:
            print("등록된 계좌가 없습니다. add-account 로 추가하세요.")
        for item in found:
            print(f"  {item['id']:3d}  {item['label']:20s} {item['institution']:12s} "
                  f"{item['type_label']}"
                  + (f"  매수 불가: {', '.join(item['gates'])}" if item["gates"] else ""))


if __name__ == "__main__":
    main()
