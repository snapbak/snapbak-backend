"""A regenerate writes the minutes again from the transcript as it stands, with what the user wrote or edited sent
to the model as facts to keep (MOM-09): each section sees its own."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from snapbak_backend.ai.prompts import minutes as minutes_prompt
from snapbak_backend.schemas.cloud import ActionItemOut
from snapbak_backend.services import minutes

MINE = ActionItemOut(id="a1", text="{{p:2}} will send the deck.", owner="{{p:2}}", derivedFrom=[], byUser=True)
MODELS = ActionItemOut(id="a2", text="{{p:1}} will book the room.", owner="{{p:1}}", derivedFrom=[])


def edited_doc() -> Any:
    doc = minutes.empty(["summary", "mom", "actionItems"])
    changes = {"summary": "The user's own summary.", "mom": "Model text.", "userEdited": ["summary"]}
    return doc.model_copy(update={**changes, "actionItems": [MINE, MODELS]})


def test_mom09_a_section_keeps_the_user_s_items_and_edited_text_only() -> None:
    doc = edited_doc()

    assert minutes.kept_of(doc, "actionItems") == [{"text": MINE.text, "owner": "{{p:2}}", "due": None}]
    assert minutes.kept_of(doc, "summary") == "The user's own summary."
    # The model wrote the MoM: nothing of it is the user's to keep.
    assert minutes.kept_of(doc, "mom") is None


def test_mom09_the_prompt_carries_what_to_keep_and_says_how_to_treat_it() -> None:
    kept = [{"text": MINE.text, "owner": "{{p:2}}", "due": None}]
    turns = minutes_prompt.turns("actionItems", "Weekly", [], [], kept=kept)

    assert json.loads(turns[1][1])["userWrote"] == kept
    assert '"userWrote"' in turns[0][1]
    # A section with nothing to keep says nothing of it.
    assert "userWrote" not in json.loads(minutes_prompt.turns("mom", "Weekly", [], [])[1][1])


def test_mom09_each_section_is_written_with_its_own_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def written(*args: Any, kept: Any = None, **kwargs: Any) -> dict[str, Any]:
        section = args[1]
        seen[section] = kept
        return {section: [] if section == "actionItems" else ""}

    async def saved(doc: Any) -> None:
        return None

    async def as_is(tiers: Any, doc: Any, *args: Any) -> Any:
        return doc

    monkeypatch.setattr(minutes, "write_section", written)
    monkeypatch.setattr(minutes, "complete", as_is)
    tiers: Any = SimpleNamespace(polish=SimpleNamespace(engine="fake:edit"))
    asyncio.run(minutes.run(tiers, "Weekly", [], [], {}, edited_doc(), saved))

    assert seen == {
        "summary": "The user's own summary.",
        "mom": None,
        "actionItems": [{"text": MINE.text, "owner": "{{p:2}}", "due": None}],
    }
