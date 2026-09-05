"""
Prompt rendering with Jinja2.

Jinja rather than LangChain's templating because the prompts contain JSON schema
examples full of `{` and `}`, which a ChatPromptTemplate reads as variables and
rejects. Rendered prompts must therefore be sent as a plain HumanMessage and
never passed through a LangChain prompt template.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from jinja2 import TemplateNotFound
from jinja2 import UndefinedError

from app.core.errors import PipelineError


def build_jinja_env(prompts_dir: Path) -> Environment:
    """Environment for the markdown prompt templates.

    StrictUndefined so a missing variable raises instead of silently rendering
    an empty string into a clinical prompt. autoescape stays off because these
    are plain-text prompts, not HTML: escaping would mangle the quotes and angle
    brackets in a transcript.
    """
    if not prompts_dir.is_dir():
        raise PipelineError(f"Prompts directory not found: {prompts_dir}")

    return Environment(
        loader=FileSystemLoader(str(prompts_dir)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def render_prompt(
    template_name: str,
    context: Dict[str, Any],
    prompts_dir: Path,
    env: Optional[Environment] = None,
) -> str:
    """Render one prompt template with the given context."""
    environment = env or build_jinja_env(prompts_dir)
    try:
        return environment.get_template(template_name).render(**context)
    except TemplateNotFound as exc:
        raise PipelineError(f"Prompt template not found: {template_name}") from exc
    except UndefinedError as exc:
        raise PipelineError(
            f"{template_name} needs a variable that was not supplied: {exc}") from exc
