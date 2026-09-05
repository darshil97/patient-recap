"""
Render a layer's prompt without calling the model or touching the database.

    python -m cli.render_prompt --transcript transcripts/riya_1.json \
        --age 24 --gender Female --call-date 2026-02-16

The fastest way to iterate on prompt wording: it exercises the real Jinja
rendering, so a missing variable or a broken template fails here rather than
after three paid API calls.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import List, Optional

from app.config import get_settings
from app.core.errors import AppError
from app.domain.transcripts import load_transcript
from app.pipeline.layers import LAYERS
from app.pipeline.results import PatientContext
from app.pipeline.runner import build_contexts
from app.pipeline.prompts import render_prompt


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a prompt for a transcript without calling the model.")
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--age", required=True)
    parser.add_argument("--gender", required=True)
    parser.add_argument("--call-date", required=True,
                        help="Real consultation date, YYYY-MM-DD")
    parser.add_argument("--layer", type=int, choices=sorted(LAYERS), default=1,
                        help="Which layer's prompt to render (default: 1)")
    parser.add_argument("--name", default="Unknown",
                        help="Patient name to render into the prompt "
                             "(transcripts no longer carry one)")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    settings = get_settings()

    try:
        date.fromisoformat(args.call_date)
    except ValueError:
        print(f"--call-date must be YYYY-MM-DD, got {args.call_date!r}")
        return 1

    try:
        transcript = load_transcript(args.transcript, settings.transcripts_dir)
    except AppError as exc:
        print(f"Error: {exc.message}")
        return 1

    # Transcripts are plain text and carry no patient name, so --name is the
    # only source; the pipeline itself takes the name from the database.
    patient = PatientContext(
        name=args.name or "Unknown",
        age=args.age,
        gender=args.gender,
        call_date=args.call_date,
    )
    context = dict(build_contexts(transcript, patient)[args.layer])

    # Layers 2 and 3 normally consume real upstream output; placeholders keep the
    # template renderable so its wording can still be reviewed.
    if args.layer >= 2:
        context["layer_1_output"] = json.dumps({"chief_complaints": []}, indent=2)
    if args.layer >= 3:
        context["layer_2_output"] = json.dumps({"doctor_stated_analysis": {}}, indent=2)
        context["follow_up_resolved"] = json.dumps({"type": "not_discussed"}, indent=2)

    try:
        print(render_prompt(LAYERS[args.layer].template, context,
                            settings.prompts_dir))
    except AppError as exc:
        print(f"Error: {exc.message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
