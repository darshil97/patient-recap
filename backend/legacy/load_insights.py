"""
Run the 3-layer pipeline over visits in the database and store the results in
visit_insights. Built to be watched and interrupted.

    python load_insights.py --list                 # what's done, what's pending
    python load_insights.py --all --dry-run        # plan only, no model calls
    python load_insights.py --all                  # process everything pending
    python load_insights.py --all --limit 2        # costed trial run
    python load_insights.py --all --pause          # confirm before each visit
    python load_insights.py --transcript transcripts/riya_2.json
    python load_insights.py --all --regenerate     # redo visits already done
    python load_insights.py --all --log-file run.log

Each visit is 3 model calls and commits in its own transaction, so Ctrl-C
between visits never leaves half-written data. Press Ctrl-C once to stop after
the visit in flight; press it twice to abandon the current visit immediately.

Visit date, patient age and gender all come from the database, so nothing about
the patient is hand-supplied. Visits that already have a current insight row are
skipped unless --regenerate is passed.
"""

from __future__ import annotations

import argparse
import logging
import re
import signal
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO

from dotenv import load_dotenv

import db
from gemini_llm import AllKeysExhausted
from pipeline import PatientInfo, PipelineError, build_llm, run_pipeline

# The Google client and httpx narrate every request; we render our own progress.
for noisy in ("httpx", "httpcore", "google_genai", "google.genai",
              "google_genai.models", "langchain_google_genai"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

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


def _fmt_tokens(n: Optional[int]) -> str:
    return f"{n:,}" if isinstance(n, int) else "-"


def quota_error_delay(exc: Exception) -> Optional[float]:
    """Return the API's suggested retry delay if this is a 429, else None.

    Free-tier keys are capped on requests per day per model, so once this fires
    every later call in the run will fail too. Treated as a stop signal rather
    than a per-visit failure.
    """
    text = str(exc)
    if "RESOURCE_EXHAUSTED" not in text and "429" not in text:
        return None
    match = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", text)
    if not match:
        match = re.search(r"retry in (\d+(?:\.\d+)?)s", text)
    return float(match.group(1)) if match else 0.0


def is_daily_quota(exc: Exception) -> bool:
    """True when the exhausted quota is a per-day cap rather than per-minute."""
    text = str(exc)
    return "PerDay" in text or "per_day" in text or "requests_per_day" in text


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate visit insights and load them into Postgres.")

    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--transcript", help="Process a single transcript path")
    target.add_argument("--all", action="store_true", help="Process every pending visit")
    target.add_argument("--list", action="store_true",
                        help="Show each visit's insight status, then exit")

    parser.add_argument("--regenerate", action="store_true",
                        help="Also reprocess visits that already have insights")
    parser.add_argument("--limit", type=int,
                        help="Stop after N visits (costed trial run)")
    parser.add_argument("--pause", action="store_true",
                        help="Ask for confirmation before each visit")
    parser.add_argument("--stop-on-error", action="store_true",
                        help="Abort the run on the first failure (default: skip and continue)")
    parser.add_argument("--sleep", type=float, default=0.0, metavar="SECONDS",
                        help="Pause between visits to stay under per-minute rate limits")
    parser.add_argument("--wait-on-quota", action="store_true",
                        help="On a short-lived rate limit, wait the API's suggested "
                             "delay and retry the visit once (daily caps still abort)")
    parser.add_argument("--model", default=None, help="Override the Gemini model id")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="List the work and exit without calling the model")
    parser.add_argument("--log-file", help="Append all progress output to this file")
    return parser.parse_args(argv)


def show_status(conn, report: Reporter) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.first_name, v.visit_number, v.visit_date, v.visit_type,
                   v.insights_generated,
                   (i.insight_id IS NOT NULL) AS has_insight,
                   i.follow_up_type, i.follow_up_date,
                   i.follow_up_needs_confirmation, i.total_tokens,
                   i.generated_at
            FROM visits v
            JOIN patients p USING (patient_id)
            LEFT JOIN visit_insights i ON i.visit_id = v.visit_id
            ORDER BY p.first_name, v.visit_number
        """)
        rows = cur.fetchall()

    report.line(f"{'patient':<9}{'#':<3}{'visit date':<13}{'generated':<11}"
                f"{'follow-up':<18}{'due':<13}{'confirm':<9}tokens")
    report.line("-" * 84)
    drift = []
    for r in rows:
        report.line(
            f"{r['first_name']:<9}{r['visit_number']:<3}"
            f"{str(r['visit_date']):<13}"
            f"{('yes' if r['insights_generated'] else 'PENDING'):<11}"
            f"{str(r['follow_up_type'] or '-'):<18}"
            f"{str(r['follow_up_date'] or '-'):<13}"
            f"{('yes' if r['follow_up_needs_confirmation'] else '-'):<9}"
            f"{_fmt_tokens(r['total_tokens'])}")
        if r["insights_generated"] != r["has_insight"]:
            drift.append(f"{r['first_name']} visit {r['visit_number']}: "
                         f"insights_generated={r['insights_generated']} but "
                         f"insight row {'exists' if r['has_insight'] else 'is missing'}")

    done = sum(1 for r in rows if r["insights_generated"])
    total = sum(r["total_tokens"] or 0 for r in rows)
    report.line("-" * 84)
    report.line(f"{done}/{len(rows)} visits generated | {len(rows) - done} pending "
                f"| {_fmt_tokens(total)} tokens used so far")

    if drift:
        report.line("\nFlag disagrees with the data for:")
        for item in drift:
            report.line(f"  ! {item}")
        report.line("  These are treated as pending and will be regenerated.")
    return 0


def check_against_next_visit(
    visit: db.VisitRow,
    due: Optional[str],
    next_dates: Dict[Any, date],
) -> Optional[str]:
    """Compare the resolved follow-up date to when the patient actually returned.

    The seeded visit dates were chained from each transcript's stated interval,
    so for any visit with a successor these should agree. A mismatch means the
    extraction or the seed data is wrong.
    """
    actual = next_dates.get((visit.patient_id, visit.visit_number + 1))
    if actual is None:
        return "no later visit on record (last in series)"
    if due is None:
        return f"MISMATCH: no date resolved, but patient returned {actual}"
    if str(due) == str(actual):
        return f"matches visit {visit.visit_number + 1} actual date {actual}"
    return f"MISMATCH: resolved {due} but visit {visit.visit_number + 1} was {actual}"


def make_progress(report: Reporter):
    """Render pipeline events as indented progress lines."""
    def on_event(event: Dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "layer_start":
            report.line(f"        layer {event['number']} {event['name']:<22} "
                        f"calling model...")
        elif kind == "layer_done":
            usage = event.get("usage") or {}
            report.line(f"        layer {event['number']} {event['name']:<22} "
                        f"{event['seconds']:5.1f}s  "
                        f"{_fmt_tokens(usage.get('total_tokens')):>8} tok  "
                        f"prompt {event['prompt_chars']:,} chars")
        elif kind == "follow_up":
            report.line(f"        follow-up: {event['summary']}")
            for warning in event.get("warnings") or []:
                report.line(f"        ! {warning}")
    return on_event


def main(argv: Optional[List[str]] = None) -> int:
    # override=True: the project's .env is authoritative. Without it a stale
    # GOOGLE_API_KEY exported in the shell silently shadows the real one.
    load_dotenv(override=True)
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="        ! %(message)s")

    report = Reporter(Path(args.log_file) if args.log_file else None)
    _install_sigint_handler(report)

    try:
        conn = db.connect()
    except Exception as exc:
        report.line(f"Error: could not connect to the database: {exc}")
        report.close()
        return 1

    try:
        with conn:
            if args.list:
                return show_status(conn, report)

            try:
                visits = db.fetch_visits(
                    conn,
                    transcript_path=args.transcript,
                    pending_only=not args.regenerate,
                )
            except db.DatabaseError as exc:
                report.line(f"Error: {exc}")
                return 1

            if args.transcript and not visits:
                report.line(
                    f"Nothing to do: {db.normalise_path(args.transcript)} already has "
                    "a current insight row. Pass --regenerate to redo it, or check "
                    "the path exists in the visits table with --list.")
                return 0
            if not visits:
                report.line("Nothing to do: every visit already has current insights. "
                            "Pass --regenerate to rebuild them.")
                return 0

            if args.limit:
                visits = visits[:args.limit]

            # For the resolved-date sanity check.
            next_dates = {
                (v.patient_id, v.visit_number): v.visit_date
                for v in db.fetch_visits(conn)
            }

            report.line(f"{len(visits)} visit(s) to process = "
                        f"{len(visits) * 3} model calls")
            for i, v in enumerate(visits, 1):
                report.line(f"  {i:>2}. {v.label:<32} {v.transcript_path}")
            report.line()

            if args.dry_run:
                report.line("Dry run: no model calls made, nothing written.")
                return 0

            try:
                llm = build_llm(model=args.model, temperature=args.temperature)
            except PipelineError as exc:
                report.line(f"Error: {exc}")
                return 1

            progress = make_progress(report)
            saved, failed, skipped = 0, 0, 0
            tokens_total = 0
            run_started = time.monotonic()

            for index, visit in enumerate(visits, 1):
                if _STOP_REQUESTED:
                    skipped = len(visits) - index + 1
                    report.line(f"\nStopped before {visit.label}. "
                                f"{skipped} visit(s) not processed.")
                    break

                report.line(f"[{index:>2}/{len(visits)}] {visit.label}  "
                            f"{visit.transcript_path}")

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

                def process_one(v: db.VisitRow) -> int:
                    """Run the chain for one visit and save it. Returns tokens used."""
                    result = run_pipeline(
                        v.transcript_path,
                        PatientInfo(
                            age=v.age_at_visit,
                            gender=v.prompt_gender,
                            call_date=v.visit_date.isoformat(),
                            name=v.first_name,
                        ),
                        llm=llm,
                        progress=progress,
                    )
                    due = result.follow_up.due_earliest if result.follow_up else None
                    report.line(f"        check: "
                                f"{check_against_next_visit(v, due, next_dates)}")
                    written = db.save_insights(
                        conn, v, result,
                        primary_diagnosis=db.transcript_diagnosis(v.transcript_path),
                    )
                    used = result.total_usage().get("total_tokens") or 0
                    report.line(f"        {written.verb}: {written.insight_id}  "
                                f"({_fmt_tokens(used)} tok, "
                                f"visits.insights_generated = TRUE)")
                    return used

                started = time.monotonic()
                quota_stop = False
                try:
                    try:
                        used = process_one(visit)
                    except Exception as exc:
                        delay = quota_error_delay(exc)
                        retryable = (delay is not None and args.wait_on_quota
                                     and not is_daily_quota(exc))
                        if not retryable:
                            raise
                        wait = max(delay or 0.0, 1.0) + 1
                        report.line(f"        rate limited; waiting {wait:.0f}s "
                                    f"then retrying this visit once")
                        time.sleep(wait)
                        used = process_one(visit)

                    tokens_total += used
                    report.line(f"        took {time.monotonic() - started:.1f}s")
                    saved += 1

                except KeyboardInterrupt:
                    report.line(f"        ABANDONED mid-visit. Nothing was written for "
                                f"{visit.label}; re-run to pick it up.")
                    skipped = len(visits) - index
                    break

                except Exception as exc:
                    failed += 1
                    if isinstance(exc, AllKeysExhausted) or quota_error_delay(exc):
                        report.line(f"        EVERY API KEY IS EXHAUSTED "
                                    f"({llm.key_count} configured).")
                        for label, state in llm.status().items():
                            report.line(f"          {label}: {state}")
                        report.line("        The free tier allows 20 requests per day "
                                    "per model per project, and each visit costs 3.")
                        report.line("        Options:")
                        report.line("          - wait for the daily reset, then re-run "
                                    "this same command (finished visits are skipped)")
                        report.line("          - switch model; each model has its own "
                                    "daily bucket:")
                        report.line("              python load_insights.py --all "
                                    "--model gemini-3.7-flash")
                        report.line("          - add another key as GOOGLE_API_KEY_4")
                        report.line("          - enable billing for higher limits")
                        quota_stop = True
                    else:
                        report.line(f"        FAILED ({type(exc).__name__}): {exc}")
                        if args.stop_on_error:
                            report.line("\nAborting on first error (--stop-on-error).")
                            break
                        report.line("        continuing to the next visit.")

                if quota_stop:
                    skipped = len(visits) - index
                    report.line(f"\nStopping: every remaining call would fail the same "
                                f"way. {skipped} visit(s) not processed.")
                    break

                report.line()
                if args.sleep and index < len(visits):
                    time.sleep(args.sleep)

            elapsed = time.monotonic() - run_started
            report.line("-" * 60)
            report.line(f"saved {saved} | failed {failed} | not processed {skipped}")
            report.line(f"tokens {_fmt_tokens(tokens_total)} | {elapsed:.0f}s total")
            report.line("api keys: " + ", ".join(
                f"{label} {state}" for label, state in llm.status().items()))
            if failed or skipped:
                report.line("Re-run the same command to pick up whatever is left; "
                            "completed visits are skipped automatically.")
            return 0 if failed == 0 else 1
    finally:
        report.close()


if __name__ == "__main__":
    raise SystemExit(main())
