"""
Batch runner: generate insights for every visit that still needs them.

    python -m cli.load_insights --list
    python -m cli.load_insights --all --dry-run
    python -m cli.load_insights --all
    python -m cli.load_insights --all --limit 2
    python -m cli.load_insights --all --pause
    python -m cli.load_insights --visit <visit_id>
    python -m cli.load_insights --transcript transcripts/riya_2.json
    python -m cli.load_insights --all --regenerate --log-file run.log

Goes through the same service layer as the API, so the two cannot drift apart.
Each visit is three model calls and commits in its own transaction, so Ctrl-C
between visits never leaves half-written data. Press Ctrl-C once to stop after
the visit in flight, twice to abandon it immediately.
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO

from app.core.errors import AppError, ConflictError, QuotaExhaustedError
from app.core.logging import configure_logging
from app.db.models import VisitRow
from app.db.pool import close_pool, connection
from app.db.repositories import visits as visits_repo
from app.llm.gemini import build_llm
from app.services import insight_service

# Set when the user presses Ctrl-C: stop after the current visit finishes.
_STOP_REQUESTED = False


class Reporter:
    """Writes progress to stdout and, optionally, a log file. Always flushed."""

    def __init__(self, log_file: Optional[Path] = None) -> None:
        self._fh: Optional[TextIO] = None
        if log_file:
            self._fh = log_file.open("a", encoding="utf-8")
            self.line(f"=== run started {date.today().isoformat()} ===")

    def line(self, text: str = "") -> None:
        print(text, flush=True)
        if self._fh:
            self._fh.write(text + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def _install_sigint_handler(report: Reporter) -> None:
    def handler(signum, frame):  # noqa: ARG001
        global _STOP_REQUESTED
        if _STOP_REQUESTED:
            report.line("\n  Second interrupt: abandoning the current visit now.")
            raise KeyboardInterrupt
        _STOP_REQUESTED = True
        report.line("\n  Interrupt received. Finishing the visit in flight, then "
                    "stopping. Press Ctrl-C again to abandon it immediately.")

    signal.signal(signal.SIGINT, handler)


def _tokens(n: Optional[int]) -> str:
    return f"{n:,}" if isinstance(n, int) else "-"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate visit insights in bulk and load them into Postgres.")

    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="Process every pending visit")
    target.add_argument("--visit", help="Process one visit by id")
    target.add_argument("--transcript", help="Process the visit with this transcript path")
    target.add_argument("--list", action="store_true",
                        help="Show each visit's status, then exit")

    parser.add_argument("--regenerate", action="store_true",
                        help="Also reprocess visits that already have insights")
    parser.add_argument("--limit", type=int, help="Stop after N visits (costed trial)")
    parser.add_argument("--pause", action="store_true",
                        help="Ask for confirmation before each visit")
    parser.add_argument("--stop-on-error", action="store_true",
                        help="Abort on the first failure (default: skip and continue)")
    parser.add_argument("--sleep", type=float, default=0.0, metavar="SECONDS",
                        help="Pause between visits to stay under per-minute limits")
    parser.add_argument("--model", default=None, help="Override the Gemini model id")
    parser.add_argument("--stop-after", type=int, choices=(1, 2, 3), default=3,
                        help="Highest layer to run (default: 3)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List the work and exit without calling the model")
    parser.add_argument("--log-file", help="Append all progress output to this file")
    return parser.parse_args(argv)


def show_status(conn, report: Reporter) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.first_name, v.visit_number, v.visit_date, v.visit_status,
                   v.transcript_path IS NOT NULL AS has_transcript,
                   v.insights_generated,
                   (i.insight_id IS NOT NULL) AS has_row,
                   i.follow_up_type, i.follow_up_date, i.total_tokens
            FROM visits v
            JOIN patients p USING (patient_id)
            LEFT JOIN visit_insights i ON i.visit_id = v.visit_id
            ORDER BY p.first_name, v.visit_number
        """)
        rows = cur.fetchall()

    report.line(f"{'patient':<9}{'#':<3}{'visit date':<13}{'transcript':<12}"
                f"{'generated':<11}{'follow-up':<18}{'due':<13}tokens")
    report.line("-" * 90)
    drift = []
    for r in rows:
        report.line(
            f"{r['first_name']:<9}{r['visit_number']:<3}{str(r['visit_date']):<13}"
            f"{('yes' if r['has_transcript'] else 'MISSING'):<12}"
            f"{('yes' if r['insights_generated'] else 'PENDING'):<11}"
            f"{str(r['follow_up_type'] or '-'):<18}"
            f"{str(r['follow_up_date'] or '-'):<13}"
            f"{_tokens(r['total_tokens'])}")
        if r["insights_generated"] != r["has_row"]:
            drift.append(f"{r['first_name']} visit {r['visit_number']}: flag="
                         f"{r['insights_generated']} but row "
                         f"{'exists' if r['has_row'] else 'missing'}")

    done = sum(1 for r in rows if r["insights_generated"])
    total = sum(r["total_tokens"] or 0 for r in rows)
    report.line("-" * 90)
    report.line(f"{done}/{len(rows)} visits generated | {len(rows) - done} pending "
                f"| {_tokens(total)} tokens used so far")
    if drift:
        report.line("\nFlag disagrees with the data for:")
        for item in drift:
            report.line(f"  ! {item}")
        report.line("  These are treated as pending and will be regenerated.")
    return 0


def make_progress(report: Reporter):
    def on_event(event: Dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "layer_start":
            report.line(f"        layer {event['number']} {event['name']:<22} "
                        f"calling model...")
        elif kind == "layer_done":
            usage = event.get("usage") or {}
            report.line(f"        layer {event['number']} {event['name']:<22} "
                        f"{event['seconds']:5.1f}s  "
                        f"{_tokens(usage.get('total_tokens')):>8} tok  "
                        f"prompt {event['prompt_chars']:,} chars")
        elif kind == "follow_up":
            report.line(f"        follow-up: {event['summary']}")
            for warning in event.get("warnings") or []:
                report.line(f"        ! {warning}")
    return on_event


def select_visits(conn, args: argparse.Namespace) -> List[VisitRow]:
    if args.visit:
        visit = visits_repo.get(conn, args.visit)
        return [visit] if visit else []
    if args.transcript:
        from app.config import get_settings
        from app.domain.transcripts import normalise_path

        path = normalise_path(Path(args.transcript),
                              get_settings().transcripts_dir.parent)
        visit = visits_repo.get_by_transcript_path(conn, path)
        return [visit] if visit else []
    return visits_repo.list_visits(conn, pending_only=not args.regenerate)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging(level=logging.WARNING)

    report = Reporter(Path(args.log_file) if args.log_file else None)
    _install_sigint_handler(report)

    try:
        with connection() as conn:
            if args.list:
                return show_status(conn, report)

            visits = select_visits(conn, args)
            visits = [v for v in visits if v is not None]

            if not visits:
                report.line("Nothing to do: no visit matched, or every visit already "
                            "has insights. Pass --regenerate to rebuild, or use "
                            "--list to see the current state.")
                return 0

            if args.limit:
                visits = visits[:args.limit]

            report.line(f"{len(visits)} visit(s) to process = "
                        f"{len(visits) * args.stop_after} model calls")
            for i, v in enumerate(visits, 1):
                marker = "" if v.transcript_path else "   [NO TRANSCRIPT]"
                report.line(f"  {i:>2}. {v.label:<32} "
                            f"{v.transcript_path or '-'}{marker}")
            report.line()

            if args.dry_run:
                report.line("Dry run: no model calls made, nothing written.")
                return 0

            llm = build_llm(model=args.model)
            progress = make_progress(report)
            saved = failed = skipped = 0
            tokens_total = 0
            run_started = time.monotonic()

            for index, visit in enumerate(visits, 1):
                if _STOP_REQUESTED:
                    skipped = len(visits) - index + 1
                    report.line(f"\nStopped before {visit.label}. "
                                f"{skipped} visit(s) not processed.")
                    break

                report.line(f"[{index:>2}/{len(visits)}] {visit.label}  "
                            f"{visit.transcript_path or '(no transcript)'}")

                if args.pause:
                    try:
                        answer = input("        process this visit? [Y/n/q] ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        answer = "q"
                    if answer == "q":
                        report.line("        quitting at user request.")
                        skipped = len(visits) - index + 1
                        break
                    if answer.startswith("n"):
                        report.line("        skipped.")
                        skipped += 1
                        continue

                started = time.monotonic()
                try:
                    outcome = insight_service.generate_for_visit(
                        conn, visit,
                        llm=llm,
                        stop_after=args.stop_after,
                        regenerate=True,  # selection already decided what to run
                        progress=progress,
                    )
                    due = outcome.follow_up.due_earliest if outcome.follow_up else None
                    report.line("        check: " + insight_service
                                .check_against_next_visit(conn, visit, due))
                    tokens_total += outcome.total_tokens or 0
                    report.line(f"        {outcome.saved.verb}: "
                                f"{outcome.saved.insight_id}  "
                                f"({_tokens(outcome.total_tokens)} tok, "
                                f"{time.monotonic() - started:.1f}s)")
                    saved += 1

                except KeyboardInterrupt:
                    report.line("        ABANDONED mid-visit. Nothing was written for "
                                f"{visit.label}; re-run to pick it up.")
                    skipped = len(visits) - index
                    break

                except QuotaExhaustedError as exc:
                    failed += 1
                    report.line(f"        EVERY API KEY IS EXHAUSTED "
                                f"({llm.key_count} configured).")
                    for label, state in llm.status().items():
                        report.line(f"          {label}: {state}")
                    report.line("        Free tier allows 20 requests per day per "
                                "model per project; each visit costs 3.")
                    report.line("        Options: wait for the daily reset and re-run "
                                "(finished visits are skipped); try another model with "
                                "--model, which has its own bucket; add another key as "
                                "GOOGLE_API_KEY_4; or enable billing.")
                    report.line(f"        detail: {exc.message[:200]}")
                    skipped = len(visits) - index
                    report.line("\nStopping: every remaining call would fail the same "
                                f"way. {skipped} visit(s) not processed.")
                    break

                except (ConflictError, AppError) as exc:
                    failed += 1
                    report.line(f"        FAILED ({exc.code}): {exc.message}")
                    if args.stop_on_error:
                        report.line("\nAborting on first error (--stop-on-error).")
                        break
                    report.line("        continuing to the next visit.")

                except Exception as exc:  # pragma: no cover
                    failed += 1
                    report.line(f"        FAILED ({type(exc).__name__}): {exc}")
                    if args.stop_on_error:
                        report.line("\nAborting on first error (--stop-on-error).")
                        break

                report.line()
                if args.sleep and index < len(visits):
                    time.sleep(args.sleep)

            report.line("-" * 60)
            report.line(f"saved {saved} | failed {failed} | not processed {skipped}")
            report.line(f"tokens {_tokens(tokens_total)} | "
                        f"{time.monotonic() - run_started:.0f}s total")
            report.line("api keys: " + ", ".join(
                f"{label} {state}" for label, state in llm.status().items()))
            if failed or skipped:
                report.line("Re-run the same command to pick up whatever is left; "
                            "completed visits are skipped automatically.")
            return 0 if failed == 0 else 1
    finally:
        report.close()
        close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
