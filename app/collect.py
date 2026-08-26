"""Manual collect: `uv run python -m app.collect [--force]`.

The web server auto-collects via the scheduler, so this is only for manual
one-off refreshes. By default it respects freshness (skips up-to-date data);
use --force to collect everything.
"""
from __future__ import annotations

import argparse

from app import db, history_recovery, registry


def main() -> None:
    parser = argparse.ArgumentParser(description="Run money data collectors")
    parser.add_argument("--force", action="store_true", help="ignore cadence/freshness")
    parser.add_argument("--job", help="run one collector by name")
    parser.add_argument(
        "--repair", action="store_true",
        help="audit cached coverage and repair deficits with persistent backoff",
    )
    parser.add_argument(
        "--history", action="store_true",
        help="run one bounded second-line historical recovery batch",
    )
    parser.add_argument(
        "--history-reset", action="store_true",
        help="explicitly re-arm historical terminal targets",
    )
    parser.add_argument("--history-kind", help="limit reset to one recovery kind")
    parser.add_argument("--history-target", help="limit reset to one target key")
    args = parser.parse_args()
    db.init_db()
    scheduler = registry.build_scheduler()
    selected = [job for job in scheduler.collectors if not args.job or job.name == args.job]
    if args.job and not selected:
        parser.error(f"unknown job: {args.job}")
    modes = sum(bool(value) for value in (
        args.repair, args.history, args.history_reset, args.force or args.job,
    ))
    if modes > 1:
        parser.error("choose only one of --repair, --history, --history-reset, --force/--job")
    if (args.history_kind or args.history_target) and not args.history_reset:
        parser.error("--history-kind/--history-target require --history-reset")
    if args.repair:
        print(scheduler.reconcile())
    elif args.history:
        print(history_recovery.run())
    elif args.history_reset:
        print({
            "reset": history_recovery.reset(
                kind=args.history_kind, target=args.history_target
            )
        })
    elif args.force or args.job:
        for job in selected:
            print(f"running {job.name}...")
            job.execute(trigger="manual")
            print(db.get_collector_state(job.name))
    else:
        scheduler.run_due()
        print("Ran due collectors (respecting freshness). Use --force to collect all.")


if __name__ == "__main__":
    main()
