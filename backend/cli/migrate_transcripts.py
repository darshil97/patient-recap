"""
Move stored transcripts between backends.

    python -m cli.migrate_transcripts --status
    python -m cli.migrate_transcripts --to neon_object_storage --dry-run
    python -m cli.migrate_transcripts --to neon_object_storage
    python -m cli.migrate_transcripts --to local              # roll back
    python -m cli.migrate_transcripts --verify
    python -m cli.migrate_transcripts --prune-local           # after verifying

WHY THIS IS A SEPARATE, EXPLICIT STEP

Changing `TRANSCRIPT_STORAGE` only changes where the *next* upload goes. Existing
visits keep reading through the provider recorded on their own row, so nothing
breaks and nothing moves. Moving them is this script's job, and it is deliberately
not a side effect of a setting or a startup hook: these files are the source
evidence behind generated clinical briefs, and §8.19's reasoning applies — a
silent, automatic rewrite of clinical source data is worse than a manual step.

THE SAFETY PROPERTY

Per visit, in this order:

    1. read from the source backend
    2. write to the destination backend
    3. read *back* from the destination and compare text exactly
    4. only then flip visits.transcript_provider

If any step fails the row is untouched and still points at the source, which still
has the bytes. Nothing is deleted: after a successful run both copies exist, the
row names the new one, and the old one is redundant but harmless. `--prune-local`
removes the leftovers as a separate decision, and refuses to run on any visit that
is not verifiably readable from its new home first.

Idempotent and resumable. A visit already on the destination is skipped, so an
interrupted run is finished by re-running the same command.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Dict, List, Optional, Tuple

from app.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.db.pool import close_pool, connection
from app.db.repositories import visits as visits_repo
from app.db.tenancy import ALL_DOCTORS
from app.storage import LOCAL, NEON_OBJECT_STORAGE, PROVIDERS, get_store

logger = logging.getLogger(__name__)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Move visit transcripts between storage backends.")

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true",
                        help="Show where every transcript currently lives, then exit")
    action.add_argument("--to", choices=PROVIDERS,
                        help="Migrate every transcript to this backend")
    action.add_argument("--verify", action="store_true",
                        help="Check every visit's transcript is readable from the "
                             "backend its row names")
    action.add_argument("--prune-local", action="store_true",
                        help="Delete local files for visits that have been migrated "
                             "away and verified readable at their new home")

    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change, write nothing")
    parser.add_argument("--limit", type=int,
                        help="Stop after N visits (useful for a first careful pass)")
    parser.add_argument("--visit", help="Operate on one visit id only")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _with_transcripts(conn, one_visit: Optional[str]) -> List[Any]:
    """Every visit that has a transcript attached.

    ALL_DOCTORS on purpose, and this is one of the few places that is correct: a
    storage migration is an operator task over the whole database, and doing it
    per doctor would leave other doctors' transcripts stranded on a backend the
    application is no longer writing to. See §8.21.
    """
    visits = visits_repo.list_visits(conn, ALL_DOCTORS, has_transcript=True,
                                     sort="date_asc")
    if one_visit:
        visits = [v for v in visits if v.visit_id == one_visit]
    return visits


def _provider_of(visit: Any) -> str:
    return (visit.transcript_provider or LOCAL).strip().lower()


def show_status(conn, one_visit: Optional[str]) -> int:
    visits = _with_transcripts(conn, one_visit)
    settings = get_settings()

    print(f"configured default for new uploads: {settings.transcript_storage}")
    if settings.transcript_storage == NEON_OBJECT_STORAGE:
        print(f"bucket: {settings.transcript_bucket}  "
              f"endpoint: {settings.s3_endpoint_url or '(unset)'}")
        print(f"credentials configured: {settings.object_storage_configured}")
    print()

    by_provider: Dict[str, int] = {}
    for v in visits:
        by_provider[_provider_of(v)] = by_provider.get(_provider_of(v), 0) + 1

    print(f"{len(visits)} visit(s) with a transcript attached:")
    for provider, count in sorted(by_provider.items()):
        print(f"  {provider:22} {count}")
    print()

    print(f"{'patient':<10}{'#':<3}{'provider':<22}{'path':<54}readable")
    print("-" * 104)
    problems = 0
    for v in visits:
        provider = _provider_of(v)
        try:
            store = get_store(provider)
            ok = "yes" if store.exists(v.transcript_path) else "MISSING"
        except AppError as exc:
            ok = f"ERROR: {exc.code}"
        if ok != "yes":
            problems += 1
        print(f"{(v.first_name or '?'):<10}{v.visit_number:<3}{provider:<22}"
              f"{(v.transcript_path or '-'):<54}{ok}")

    print("-" * 104)
    if problems:
        print(f"{problems} transcript(s) are not readable from the backend their "
              f"row names. Fix before migrating.")
    return 1 if problems else 0


def verify(conn, one_visit: Optional[str]) -> int:
    """Confirm every row's transcript is readable where the row says it is."""
    visits = _with_transcripts(conn, one_visit)
    bad: List[Tuple[str, str]] = []

    for v in visits:
        provider = _provider_of(v)
        try:
            stored = get_store(provider).read(v.transcript_path)
            if not stored.text.strip():
                bad.append((v.label, "readable but empty"))
        except AppError as exc:
            bad.append((v.label, f"{exc.code}: {exc.message}"))

    print(f"verified {len(visits)} transcript(s)")
    for label, why in bad:
        print(f"  FAIL {label}: {why}")
    if not bad:
        print("all readable from the backend their row names")
    return 1 if bad else 0


def migrate(conn, destination: str, dry_run: bool, limit: Optional[int],
            one_visit: Optional[str]) -> int:
    visits = _with_transcripts(conn, one_visit)
    todo = [v for v in visits if _provider_of(v) != destination]
    already = len(visits) - len(todo)

    print(f"{len(visits)} visit(s) with a transcript | {already} already on "
          f"{destination} | {len(todo)} to move")
    if limit:
        todo = todo[:limit]
        print(f"--limit {limit}: processing {len(todo)}")
    if dry_run:
        print("\nDRY RUN — nothing will be written.\n")
    print()

    destination_store = get_store(destination)
    moved = failed = 0

    for index, visit in enumerate(todo, 1):
        source = _provider_of(visit)
        locator = visit.transcript_path
        prefix = f"[{index:>2}/{len(todo)}] {visit.label:<34}"

        try:
            original = get_store(source).read(locator)
        except AppError as exc:
            print(f"{prefix} SKIP  unreadable at source ({source}): {exc.message}")
            failed += 1
            continue

        if dry_run:
            print(f"{prefix} would move {source} -> {destination} "
                  f"({len(original.text):,} chars)")
            continue

        try:
            destination_store.write(locator, original.text)

            # Read back and compare before touching the row. A write that
            # "succeeded" but stored something different is the failure mode worth
            # spending a round trip to rule out, because the row would then point
            # at a corrupted clinical source and nothing would notice.
            check = destination_store.read(locator)
            if check.text != original.text:
                raise AppError(
                    f"verification failed: {len(original.text)} chars written, "
                    f"{len(check.text)} read back")

            # set_transcript_provider, NOT set_transcript. The latter stamps
            # transcript_uploaded_at = now(), which the UI reads as "the
            # transcript was replaced" and uses to warn that a brief is out of
            # date. Moving bytes between backends changes no words, so claiming
            # an upload put a false clinical-safety warning on every migrated
            # brief the first time this ran.
            updated = visits_repo.set_transcript_provider(
                conn, ALL_DOCTORS, visit.visit_id,
                transcript_provider=destination,
            )
            if updated is None or _provider_of(updated) != destination:
                raise AppError("the visit row did not record the new provider")

            print(f"{prefix} moved {source} -> {destination} "
                  f"({len(original.text):,} chars, verified)")
            moved += 1

        except AppError as exc:
            print(f"{prefix} FAILED {exc.message}")
            print(f"{'':<48} row untouched, still reading from {source}")
            failed += 1
        except Exception as exc:  # pragma: no cover - unexpected
            print(f"{prefix} FAILED {type(exc).__name__}: {exc}")
            print(f"{'':<48} row untouched, still reading from {source}")
            failed += 1

    print()
    print("-" * 70)
    if dry_run:
        print(f"dry run: {len(todo)} visit(s) would move to {destination}")
        return 0
    print(f"moved {moved} | failed {failed} | already there {already}")
    if moved:
        print(f"\nBoth copies now exist. The rows read from {destination}; the old "
              f"copies are redundant.")
        if destination == NEON_OBJECT_STORAGE:
            print("Run --verify, then --prune-local to remove the local files.")
    if failed:
        print("Re-run the same command to retry the failures.")
    return 0 if failed == 0 else 1


def prune_local(conn, dry_run: bool, one_visit: Optional[str]) -> int:
    """Delete local files for visits that have moved away and verify first.

    Separate from `migrate` because it is the only destructive step, and because
    the safe moment to run it is after a verification pass, not in the same breath
    as the copy.
    """
    visits = _with_transcripts(conn, one_visit)
    candidates = [v for v in visits if _provider_of(v) != LOCAL]

    print(f"{len(candidates)} visit(s) no longer read from local storage")
    if dry_run:
        print("\nDRY RUN — nothing will be deleted.\n")

    local_store = get_store(LOCAL)
    removed = skipped = 0

    for visit in candidates:
        locator = visit.transcript_path
        if not local_store.exists(locator):
            continue

        # Refuse to delete the local copy unless the new home is readable. This is
        # the check that makes pruning safe to run without thinking hard about it.
        try:
            stored = get_store(_provider_of(visit)).read(locator)
            if not stored.text.strip():
                raise AppError("new copy is empty")
        except AppError as exc:
            print(f"  KEEP {visit.label}: not verifiable at "
                  f"{_provider_of(visit)} ({exc.message})")
            skipped += 1
            continue

        if dry_run:
            print(f"  would delete local copy of {locator}")
            continue

        local_store.delete(locator)
        print(f"  deleted local copy of {locator}")
        removed += 1

    print()
    print(f"deleted {removed} local file(s) | kept {skipped} that could not be "
          f"verified elsewhere")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging(level=logging.WARNING)

    try:
        with connection() as conn:
            if args.status:
                return show_status(conn, args.visit)
            if args.verify:
                return verify(conn, args.visit)
            if args.prune_local:
                return prune_local(conn, args.dry_run, args.visit)
            return migrate(conn, args.to, args.dry_run, args.limit, args.visit)
    finally:
        close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
