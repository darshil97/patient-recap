"""
One-off: convert the seeded JSON transcripts to .txt and repoint the database.

    python -m cli.convert_transcripts --dry-run
    python -m cli.convert_transcripts
    python -m cli.convert_transcripts --delete-originals

The 16 JSON files were a by-product of generating synthetic transcripts. Going
forward transcripts are plain .txt uploaded by the doctor, so this brings the
existing data onto the same convention: `transcripts/{visit_id}.txt`.

Reads the JSON with `json.load` directly rather than through
`domain.transcripts.load_transcript`, so it does not depend on whether the
.txt-only loader has shipped. Run this first, then deploy the loader change.

Each visit is converted in its own transaction, so an interrupt cannot leave a
row pointing at a file that was never written.

TENANCY: this script is cross-doctor and stays that way. It predates
multi-tenancy, has already been run against the only data it was written for (the
16 seeded visits, all now owned by the first doctor), and touches nothing but
`transcript_path` / `transcript_filename` — no patient or clinical content. It
talks to Postgres in raw SQL rather than through the repositories, so it is
untouched by the scoping change; if it is ever run again on a multi-doctor
database, note that it would rewrite transcript paths for every doctor at once.
See §8.20 for why CLI tooling is privileged by design.
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from app.config import BASE_DIR, get_settings
from app.core.errors import TranscriptError
from app.db.pool import close_pool, connection
from app.domain.transcripts import resolve_within

ARCHIVE_DIR = BASE_DIR / "archive" / "transcripts_json"


@dataclass
class Outcome:
    visit_id: str
    label: str
    source: Optional[Path] = None
    target: Optional[Path] = None
    characters: int = 0
    status: str = "pending"      # converted | skipped | failed | would_convert
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("converted", "would_convert")


def find_source(transcript_path: str, transcripts_dir: Path) -> Path:
    """Locate the JSON file, falling back to the archive.

    The fallback makes this re-runnable on a fresh clone, where transcripts/*.txt
    is gitignored and only the archived JSON is present.
    """
    try:
        candidate = resolve_within(transcript_path, transcripts_dir)
        if candidate.is_file():
            return candidate
    except TranscriptError:
        pass  # fall through to the archive

    archived = ARCHIVE_DIR / Path(transcript_path).name
    if archived.is_file():
        return archived

    raise FileNotFoundError(
        f"neither {transcript_path} nor {archived.relative_to(BASE_DIR)} exists")


def extract_text(source: Path) -> str:
    """Pull the transcript string out of the JSON and normalise newlines."""
    with source.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    text = (data.get("transcript") or "")
    if not isinstance(text, str):
        raise ValueError("'transcript' is not a string")
    # Normalise so the written file is \n-only regardless of what the JSON held.
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def convert_one(conn, visit: dict, transcripts_dir: Path,
                dry_run: bool) -> Outcome:
    visit_id = visit["visit_id"]
    label = f"{visit['first_name']} visit {visit['visit_number']}"
    result = Outcome(visit_id=visit_id, label=label)

    try:
        source = find_source(visit["transcript_path"], transcripts_dir)
    except FileNotFoundError as exc:
        result.status, result.detail = "failed", str(exc)
        return result
    result.source = source

    try:
        text = extract_text(source)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        result.status, result.detail = "failed", str(exc)
        return result

    if not text:
        result.status = "skipped"
        result.detail = "'transcript' was missing or empty; nothing written"
        return result

    target = transcripts_dir / f"{visit_id}.txt"
    stored_path = f"{transcripts_dir.name}/{target.name}"
    filename = f"{source.stem}.txt"
    result.target, result.characters = target, len(text)

    if dry_run:
        result.status = "would_convert"
        result.detail = f"-> {stored_path} ({len(text):,} chars), filename {filename}"
        return result

    try:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")

        # Verify by re-reading rather than trusting the write.
        written = target.read_text(encoding="utf-8")
        if written != text:
            result.status = "failed"
            result.detail = (f"verification failed: wrote {len(text):,} chars, "
                             f"read back {len(written):,}")
            return result

        archived = ARCHIVE_DIR / source.name
        if source.resolve() != archived.resolve():
            shutil.copy2(source, archived)

        # One transaction per visit: the row and the file agree or neither moves.
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE visits
                       SET transcript_path        = %s,
                           transcript_filename    = %s,
                           transcript_provider    = 'local',
                           transcript_uploaded_at = now(),
                           updated_at             = now()
                       WHERE visit_id = %s""",
                    (stored_path, filename, visit_id),
                )
        result.status = "converted"
        result.detail = f"-> {stored_path} ({len(text):,} chars)"
    except OSError as exc:
        result.status, result.detail = "failed", f"write failed: {exc}"
    return result


def cleanup_originals(conn, transcripts_dir: Path, dry_run: bool) -> int:
    """Remove leftover .json files from transcripts/ after a verified conversion.

    Refuses unless every visit already points at a .txt and each file being
    removed has an archive copy, so this cannot destroy the only copy of a
    transcript.
    """
    strays = sorted(transcripts_dir.glob("*.json"))
    if not strays:
        print("No .json files left in transcripts/; nothing to clean up.")
        return 0

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM visits "
                    "WHERE transcript_path ILIKE '%%.json'")
        still_json = cur.fetchone()["n"]

    if still_json:
        print(f"Refusing to delete: {still_json} visit(s) still reference a .json "
              f"path. Run the conversion first.")
        return 1

    unarchived = [p for p in strays if not (ARCHIVE_DIR / p.name).is_file()]
    if unarchived:
        print("Refusing to delete: no archive copy exists for "
              + ", ".join(p.name for p in unarchived))
        return 1

    print(f"{len(strays)} original .json file(s) in {transcripts_dir.name}/, "
          f"all archived and unreferenced:")
    for path in strays:
        print(f"  {'would delete' if dry_run else 'deleted'}  {path.name}")
        if not dry_run:
            path.unlink()

    if dry_run:
        print("\nDry run: nothing deleted.")
    else:
        print(f"\nDeleted {len(strays)} file(s). Archive copies kept in "
              f"{ARCHIVE_DIR.relative_to(BASE_DIR)}/.")
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert seeded JSON transcripts to .txt and update the database.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing anything")
    parser.add_argument("--delete-originals", action="store_true",
                        help="Remove the .json files from transcripts/ once every "
                             "row has converted and verified (archive copy is kept)")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    transcripts_dir = get_settings().transcripts.local_dir

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT v.visit_id::text, v.visit_number, v.transcript_path,
                       p.first_name
                FROM visits v JOIN patients p USING (patient_id)
                WHERE v.transcript_path ILIKE '%%.json'
                ORDER BY p.first_name, v.visit_number
            """)
            visits = cur.fetchall()

        if not visits:
            print("Nothing to convert: no visit points at a .json transcript.")
            # Conversion and cleanup are usually separate runs: you convert,
            # check the result, then tidy up. So --delete-originals has to work
            # on its own rather than only alongside a conversion.
            if args.delete_originals:
                return cleanup_originals(conn, transcripts_dir, args.dry_run)
            return 0

        mode = "DRY RUN" if args.dry_run else "converting"
        print(f"{len(visits)} visit(s) to convert ({mode})\n")

        results = [convert_one(conn, v, transcripts_dir, args.dry_run)
                   for v in visits]

        for r in results:
            marker = {"converted": "ok  ", "would_convert": "plan",
                      "skipped": "SKIP", "failed": "FAIL"}[r.status]
            print(f"  {marker}  {r.label:<20} {r.detail}")

        converted = [r for r in results if r.ok]
        skipped = [r for r in results if r.status == "skipped"]
        failed = [r for r in results if r.status == "failed"]

        print(f"\n{len(converted)} converted, {len(skipped)} skipped, "
              f"{len(failed)} failed")

        if args.dry_run:
            print("\nDry run: nothing written, no rows changed.")
            return 0 if not failed else 1

        # Only tidy up when every single row came through.
        if args.delete_originals:
            if failed or skipped:
                print("\nNot deleting originals: some rows did not convert.")
            else:
                removed = 0
                for r in converted:
                    if r.source and r.source.parent == transcripts_dir:
                        r.source.unlink()
                        removed += 1
                print(f"\nDeleted {removed} original .json file(s) from "
                      f"{transcripts_dir.name}/ (archive copies kept).")
        elif converted:
            print(f"\nOriginals left in {transcripts_dir.name}/. Nothing reads them "
                  f"now; re-run with --delete-originals to remove them.")

        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) FILTER (WHERE transcript_path ILIKE '%%.json') AS still_json,
                       count(*) FILTER (WHERE transcript_path ILIKE '%%.txt')  AS txt
                FROM visits
            """)
            counts = cur.fetchone()
        print(f"\ndatabase now: {counts['txt']} .txt, {counts['still_json']} .json")

    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        close_pool()
