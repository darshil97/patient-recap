# Superseded modules

These are the pre-refactor scripts, kept only because this project is not under
version control yet. Nothing imports them. Every behaviour has moved into `app/`
and `cli/`:

| Old | New |
| --- | --- |
| `pipeline.py` | `app/pipeline/` (layers, prompts, parsing, results, runner) and `app/domain/follow_up/` (models, resolver) |
| `db.py` | `app/db/` (pool, models, repositories) and `app/services/insight_service.py` |
| `gemini_llm.py` | `app/llm/` (gemini, errors) |
| `load_insights.py` | `cli/load_insights.py` over `app/services/` |
| `main.py` | `cli/render_prompt.py`, plus the API in `app/main.py` |

Safe to delete once you have confirmed the new structure works. Consider
`git init` first so future refactors are recoverable.
