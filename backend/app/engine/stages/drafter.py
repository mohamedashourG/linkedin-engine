"""
Drafter: writes the comment text for an allocated candidate.

Inputs:
  - candidate (post text + author + source classification A/B + assigned comment type)
  - cofounder.voice_profile (source_a_template + source_b_template)

Source-aware: classification A → source_a_template (substantive). B → source_b_template (warm-light).

Output: a single comment string. Validation happens downstream.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.engine.constants import COMMENT_TYPE_DESCRIPTIONS
from app.services.openai_client import parse_structured_sync

log = logging.getLogger(__name__)


class _Draft(BaseModel):
    comment: str = Field(
        description=(
            "The final comment to post. No em-dashes. No sycophantic openers. "
            "Reads like a single specific human, not a brand."
        )
    )


def draft_comment(
    *,
    post_text: str,
    author_name: str | None,
    voice_template: str,
    comment_type: str,
    source_classification: str,
) -> str:
    type_desc = COMMENT_TYPE_DESCRIPTIONS.get(comment_type, "")
    system = (
        f"{voice_template}\n\n"
        f"---\n"
        f"COMMENT TYPE for this draft: {comment_type}\n{type_desc}\n\n"
        f"SOURCE: classification {source_classification} "
        f"({'tier-1 keyword match (substantive)' if source_classification == 'A' else 'tier-2/3 or harvester (warm-light)'}).\n\n"
        "Hard rules: no em-dashes (use commas/periods), 60+ characters, no sycophantic openers."
    )
    user = f"Author: {author_name or '(unknown)'}\n\nPost:\n{post_text}"
    result = parse_structured_sync(
        model_tier="primary",
        system=system,
        user=user,
        schema=_Draft,
    )
    return result.comment.strip()
