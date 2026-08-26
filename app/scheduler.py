"""Persistent cadence scheduler with deficit-driven compensating collection.

Scheduling and repair backoff state lives in SQLite, so restarts recover gaps
without repeatedly hammering providers. Collection runs outside the FastAPI
startup path. Deploy this project with one scheduler-enabled worker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

from app import db
from app.timeutil import instant_epoch


log = logging.getLogger("money")


@dataclass
class Collector:
    name: str
    interval: int
    run: Callable[[], dict]
    is_fresh: Callable[[], bool] | None = None
    repair: Callable[[], dict] | None = None
    error_interval: int | None = None

    @property
    def state(self) -> dict | None:
        value = db.get_collector_state(self.name)
        return value if isinstance(value, dict) else None

    @property
    def last_run(self) -> float:
        return instant_epoch((self.state or {}).get("last_attempt_at"))

    def due(self, now: float) -> bool:
        if now - self.last_run < self.interval:
            return False
        if self.is_fresh is not None:
            try:
                if self.is_fresh():
                    previous = self.state or {}
                    db.set_collector_state(
                        self.name,
                        status="fresh",
                        ok=int(previous.get("ok") or 0),
                        total=int(previous.get("total") or 0),
                        duration_ms=previous.get("duration_ms"),
                        details="freshness check passed; collection skipped",
                    )
                    return False
            except Exception as exc:
                # A broken freshness check must not suppress a needed run.
                log.warning("freshness check %s failed: %s", self.name, exc)
        return True

    def execute(
        self,
        *,
        trigger: str = "schedule",
        runner: Callable[[], dict] | None = None,
    ) -> dict:
        started = time.monotonic()
        try:
            result = (runner or self.run)()
            ok = int(result.get("ok", 0))
            total = int(result.get("total", 0))
            errors = dict(result.get("errors") or {})
            pending = int(result.get("pending", 0))
            if (
                trigger == "reconcile" and not errors and not pending
                and self.is_fresh is not None
            ):
                try:
                    if not self.is_fresh():
                        errors["coverage"] = "coverage audit still failing after repair"
                except Exception as exc:
                    errors["coverage_audit"] = f"{type(exc).__name__}: {exc}"
            status = (
                "success" if not errors and not pending and ok == total
                else ("partial" if ok or pending else "error")
            )
            details = json.dumps({
                "trigger": trigger,
                "errors": errors,
                "pending": pending,
            }, ensure_ascii=False, sort_keys=True)[:8000]
            duration_ms = int((time.monotonic() - started) * 1000)
            db.log_collect(
                self.name, ok, total, duration_ms,
                details=details, status=status,
            )
            db.set_collector_state(
                self.name,
                status=status,
                ok=ok,
                total=total,
                duration_ms=duration_ms,
                error=(
                    None if not errors and not pending else
                    "; ".join(filter(None, (
                        f"{len(errors)} item(s) failed" if errors else "",
                        f"{pending} item(s) pending" if pending else "",
                    )))
                ),
                details=details,
                success=status == "success",
            )
            return {
                "name": self.name,
                "trigger": trigger,
                "status": status,
                "ok": ok,
                "total": total,
                "pending": pending,
                "duration_ms": duration_ms,
            }
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            message = f"{type(exc).__name__}: {exc}"
            log.exception("collector %s failed", self.name)
            db.log_collect(
                self.name, 0, 0, duration_ms,
                details=message, status="error",
            )
            db.set_collector_state(
                self.name,
                status="error",
                duration_ms=duration_ms,
                error=message,
            )
            return {
                "name": self.name,
                "trigger": trigger,
                "status": "error",
                "ok": 0,
                "total": 0,
                "duration_ms": duration_ms,
                "error": message,
            }


class Scheduler:
    """Runs cadence jobs plus deficit-driven compensating collection."""

    def __init__(
        self,
        *,
        reconcile_interval: int | None = None,
        repair_backoff: int | None = None,
        error_backoff: int | None = None,
    ) -> None:
        self.collectors: list[Collector] = []
        self._run_lock = threading.Lock()
        self.reconcile_interval = max(30, (
            reconcile_interval if reconcile_interval is not None
            else int(os.environ.get("RECONCILE_INTERVAL", "300"))
        ))
        self.repair_backoff = max(0, (
            repair_backoff if repair_backoff is not None
            else int(os.environ.get("REPAIR_BACKOFF", "3600"))
        ))
        self.error_backoff = max(0, (
            error_backoff if error_backoff is not None
            else int(os.environ.get("REPAIR_ERROR_BACKOFF", "21600"))
        ))
        self.last_reconcile_at: str | None = db.get_meta("last_reconcile")
        self.last_reconcile_report: list[dict] = []

    def register(self, collector: Collector) -> None:
        if any(existing.name == collector.name for existing in self.collectors):
            raise ValueError(f"duplicate collector: {collector.name}")
        self.collectors.append(collector)

    def run_due(self) -> None:
        """Run due collectors once. Concurrent invocations collapse to one."""
        if not self._run_lock.acquire(blocking=False):
            return
        try:
            now = time.time()
            for collector in self.collectors:
                if collector.due(now):
                    collector.execute(trigger="schedule")
        finally:
            self._run_lock.release()

    def _recovery_delay(self, collector: Collector) -> int:
        delay = min(collector.interval, self.repair_backoff)
        status = (collector.state or {}).get("status")
        if status in {"partial", "error"}:
            error_delay = (
                collector.error_interval
                if collector.error_interval is not None else self.error_backoff
            )
            delay = max(delay, min(collector.interval, error_delay))
        return delay

    def reconcile(self, now: float | None = None) -> list[dict]:
        """Audit local coverage and repair deficits independently of cadence.

        Audits are cache-only. Provider calls happen only for collectors whose
        audit fails and whose persistent last-attempt backoff has expired.
        """
        if not self._run_lock.acquire(blocking=False):
            return [{"action": "busy", "reason": "another collection run is active"}]
        report: list[dict] = []
        try:
            current = time.time() if now is None else now
            for collector in self.collectors:
                if collector.is_fresh is None:
                    report.append({"name": collector.name, "action": "no_audit"})
                    continue
                try:
                    fresh = collector.is_fresh()
                except Exception as exc:
                    log.warning("reconciliation audit %s failed: %s", collector.name, exc)
                    report.append({
                        "name": collector.name,
                        "action": "audit_error",
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    continue
                if fresh:
                    report.append({"name": collector.name, "action": "fresh"})
                    continue
                delay = self._recovery_delay(collector)
                age = current - collector.last_run
                if collector.last_run and age < delay:
                    report.append({
                        "name": collector.name,
                        "action": "backoff",
                        "retry_in_seconds": max(0, int(delay - age)),
                    })
                    continue
                outcome = collector.execute(
                    trigger="reconcile",
                    runner=collector.repair or collector.run,
                )
                report.append({
                    "name": collector.name,
                    "action": "repaired",
                    "status": outcome["status"],
                    "ok": outcome["ok"],
                    "total": outcome["total"],
                    "pending": outcome.get("pending", 0),
                })
            self.last_reconcile_at = db.utc_now()
            self.last_reconcile_report = report
            db.set_meta("last_reconcile", self.last_reconcile_at)
            db.set_meta(
                "last_reconcile_report",
                json.dumps(report, ensure_ascii=False, sort_keys=True)[:16000],
            )
            return report
        finally:
            self._run_lock.release()

    def reconciliation_status(self) -> dict:
        report = self.last_reconcile_report
        if not report:
            try:
                report = json.loads(db.get_meta("last_reconcile_report") or "[]")
            except json.JSONDecodeError:
                report = []
        if not isinstance(report, list):
            report = []
        return {
            "last_run": self.last_reconcile_at or db.get_meta("last_reconcile"),
            "audit_interval_seconds": self.reconcile_interval,
            "repair_backoff_seconds": self.repair_backoff,
            "error_backoff_seconds": self.error_backoff,
            "report": report,
        }

    async def loop(self) -> None:
        """Reconcile on startup/interval and check cadence every 30 seconds."""
        next_reconcile = 0.0
        while True:
            now = time.time()
            if now >= next_reconcile:
                await asyncio.to_thread(self.reconcile, now)
                next_reconcile = time.time() + self.reconcile_interval
            await asyncio.to_thread(self.run_due)
            await asyncio.sleep(30)


def make_collector(
    name, interval, run, is_fresh=None, repair=None, error_interval=None,
) -> Collector:
    return Collector(
        name=name,
        interval=interval,
        run=run,
        is_fresh=is_fresh,
        repair=repair,
        error_interval=error_interval,
    )
