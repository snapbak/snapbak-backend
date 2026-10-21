"""The caption translation prompt (TRX-07, 16-caption-translation LLD, Prompt draft): a system turn, one JSON user
turn per batch, and the correction turn when an answer breaks a rule."""

import json
from typing import Any

from snapbak_backend.ai.providers.base import Turn

SYSTEM = """You translate video captions from {source_name} to {target_name}.

Return JSON only, in this shape: {{"lines":[{{"idx":<number>,"text":"<translation>"}}]}}

Rules:
- Return exactly one entry for every line in "translate", with the same idx, in the same order.
- Never merge, split, drop or add lines, even when a sentence runs across two lines.
- Lines in "before" and "after" are context only. Do not translate or return them.
- Keep each translation close to the length of its source, so it fits the same screen time. Prefer how people speak \
over word-for-word wording.
- Keep names, product names, code, file names, URLs, numbers, units and keyboard shortcuts as they are.
- Keep the terms in "keep" exactly as written.
- Keep the speaker's tone. Add no notes, quotes, brackets or explanations.
- Write {target_name} in its usual script{script_note}."""

CORRECTION = """Your answer broke these rules: {problems}.
Return JSON with exactly these idx values, in order: {idx_list}."""

type Entry = dict[str, Any]


def turns(
    source: str,
    source_name: str,
    target: str,
    target_name: str,
    script_note: str,
    keep: list[str],
    parts: tuple[list[Entry], list[Entry], list[Entry]],
) -> list[Turn]:
    """`parts` is the before, translate and after lines, each `{"idx", "text"}`."""
    before, translate, after = parts
    system = SYSTEM.format(source_name=source_name, target_name=target_name, script_note=script_note)
    request = {
        "source": source,
        "target": target,
        "keep": keep,
        "before": before,
        "translate": translate,
        "after": after,
    }
    made = [("system", system), ("user", json.dumps(request, ensure_ascii=False))]
    return made


def correction(problems: list[str], idx_list: list[int]) -> str:
    text = CORRECTION.format(problems="; ".join(problems), idx_list=json.dumps(idx_list))
    return text
