"""
CLI for the 3-layer homeopathic consultation pipeline.

    python main.py --transcript transcripts/riya_1.json \
        --age 28 --gender Female --call-date 2026-08-22
    python main.py --transcript transcripts/vikas_2.json \
        --age 41 --gender Male --call-date 2026-08-22 --output-dir output
    python main.py --transcript transcripts/kavita_1.json \
        --age 35 --gender Female --call-date 2026-08-22 --stop-after 1 --dry-run

The next visit is not an input: Layer 1 extracts what the doctor said and
resolve_follow_up() turns it into a calendar date, anchored on --call-date.

Requires GOOGLE_API_KEY (or GEMINI_API_KEY) in the environment or a .env file.
For programmatic use, import run_pipeline / PatientInfo from pipeline.py instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from typing import List, Optional

from dotenv import load_dotenv

from pipeline import (
    DEFAULT_MODEL,
    LAYERS,
    LayerOutputError,
    PatientInfo,
    PipelineError,
    build_contexts,
    build_llm,
    load_transcript,
    render_prompt,
    run_pipeline,
    save_result,
)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Layer 1-3 homeopathic consultation chain over one transcript.",
    )
    parser.add_argument("--transcript", required=True,
                        help="Path to the transcript JSON file")

    patient = parser.add_argument_group("patient metadata (not stored in transcripts)")
    patient.add_argument("--age", required=True, help="Patient age")
    patient.add_argument("--gender", required=True, help="Patient gender")
    # Required, not defaulted to today: this date anchors every relative
    # follow-up ("in three weeks"), so a guessed value yields a wrong due date.
    patient.add_argument("--call-date", required=True,
                         help="Actual consultation date, YYYY-MM-DD")
    patient.add_argument("--name", help="Override the patient name from the transcript")

    model = parser.add_argument_group("model")
    model.add_argument("--model", default=None,
                       help=f"Gemini model id (default: $GEMINI_MODEL or {DEFAULT_MODEL})")
    model.add_argument("--temperature", type=float, default=0.0,
                       help="Sampling temperature (default: 0.0 for reproducible extraction)")
    model.add_argument("--no-json-mode", action="store_true",
                       help="Don't constrain the API response to JSON")

    run = parser.add_argument_group("run control")
    run.add_argument("--stop-after", type=int, choices=sorted(LAYERS), default=3,
                     help="Highest layer to run (default: 3)")
    run.add_argument("--allow-non-json", action="store_true",
                     help="Forward a layer's raw text downstream instead of failing")
    run.add_argument("--output-dir",
                     help="Directory to save layer outputs (default: don't save)")
    run.add_argument("--save-prompts", action="store_true",
                     help="Also save each rendered prompt alongside its output")
    run.add_argument("--dry-run", action="store_true",
                     help="Render and print the Layer 1 prompt without calling the model")
    run.add_argument("--quiet", action="store_true", help="Only print the final layer's JSON")

    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    # override=True: the project's .env is authoritative. Without it a stale
    # GOOGLE_API_KEY exported in the shell silently shadows the real one.
    load_dotenv(override=True)
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
    )

    # Fail fast on a bad date rather than letting the resolver silently skip
    # every relative follow-up.
    try:
        date.fromisoformat(args.call_date)
    except ValueError:
        print(f"Error: --call-date must be YYYY-MM-DD, got {args.call_date!r}",
              file=sys.stderr)
        return 1

    patient = PatientInfo(
        age=args.age,
        gender=args.gender,
        call_date=args.call_date,
        name=args.name,
    )

    try:
        if args.dry_run:
            # Layers 2 and 3 need real upstream output, so only Layer 1 can be
            # rendered without calling the model.
            transcript = load_transcript(args.transcript)
            context = build_contexts(transcript, patient)[1]
            print(render_prompt(LAYERS[1].template, context))
            return 0

        llm = build_llm(
            model=args.model,
            temperature=args.temperature,
            json_mode=not args.no_json_mode,
        )

        result = run_pipeline(
            args.transcript,
            patient,
            llm=llm,
            stop_after=args.stop_after,
            strict_json=not args.allow_non_json,
        )
    except LayerOutputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if exc.raw_response:
            print(f"\n--- raw response ---\n{exc.raw_response[:2000]}", file=sys.stderr)
        return 1
    except PipelineError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # network / API / quota failures
        print(f"Model call failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1

    if args.quiet:
        final = result.layers[max(result.layers)]
        print(json.dumps(final.data, indent=2, ensure_ascii=False)
              if final.is_json else final.raw_response)
    else:
        print(f"\nTranscript : {result.transcript.path.name} "
              f"(patient: {patient.name or result.transcript.patient_name})")
        print(f"Call date  : {patient.call_date}")
        print(f"Model      : {result.model}")
        print(f"Tokens     : {result.total_usage() or 'n/a'}")
        if result.follow_up:
            print(f"Follow-up  : {result.follow_up.summary()}")
            for warning in result.follow_up.warnings:
                print(f"             ! {warning}")
        for number, layer in sorted(result.layers.items()):
            print(f"\n=== Layer {number}: {layer.spec.name} ===")
            print(json.dumps(layer.data, indent=2, ensure_ascii=False)
                  if layer.is_json else layer.raw_response)

    if args.output_dir:
        run_dir = save_result(result, args.output_dir, include_prompts=args.save_prompts)
        print(f"\nSaved to {run_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
