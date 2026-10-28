"""The people the minutes name (services/people.py, MEETINGS.md D41): `{{p:N}}` is the speaker labelled Speaker N and
`{{n:Name}}` someone who never speaks; the model's tokens are grounded in the speakers, and every reader that needs
words gets each person by their name now."""

from uuid import uuid4

from snapbak_backend.db.models import Speaker
from snapbak_backend.schemas.cloud import ActionItemOut, DecisionOut, DerivationOut, MinutesOut
from snapbak_backend.services import people


def speaker(n: int, name: str | None = None) -> Speaker:
    made = Speaker(id=uuid4(), recording_id=uuid4(), label=f"Speaker {n}", vendor_label="A", name=name)
    return made


JOANE, SECOND, NOAH, TENTH = speaker(1, "Joane"), speaker(2), speaker(3, "Noah"), speaker(10)
SPEAKERS = {each.id: each for each in (JOANE, SECOND, NOAH, TENTH)}


def test_mom06_a_token_for_a_speaker_stands_and_one_for_no_speaker_is_refused() -> None:
    problems: list[str] = []
    made = people.checked("{{p:1}} thanked {{p:10}} and {{p:7}}.", SPEAKERS, problems, "summary")
    assert made == "{{p:1}} thanked {{p:10}} and ."
    assert problems == ["summary named {{p:7}}, who is no speaker: use a line's p"]


def test_mom06_someone_named_by_name_who_is_a_speaker_becomes_their_token() -> None:
    problems: list[str] = []
    made = people.checked("{{n:Noah}} asked {{n: Rabbi Ian }}.", SPEAKERS, problems, "mom")
    assert (made, problems) == ("{{p:3}} asked {{n:Rabbi Ian}}.", [])


def test_mom06_rendered_words_name_each_person_as_they_are_now() -> None:
    said = people.rendered("{{p:1}}, {{p:2}} and {{p:10}} met {{n:Rabbi Ian}}; {{p:9}} left.", SPEAKERS)
    assert said == "Joane, Speaker 2 and Speaker 10 met Rabbi Ian; someone left."


def test_mom06_a_rendered_document_has_no_token_in_any_words() -> None:
    cite = DerivationOut(lineId=uuid4(), speaker="Joane", atMs=0, quote="q")
    doc = MinutesOut.model_validate(
        {
            "title": "Housewarming",
            "openQuestions": [],
            "summary": "{{p:1}} and {{p:3}} host.",
            "mom": "### Party\n\n{{p:3}} invites {{n:Rabbi Ian}}.",
            "actionItems": [
                ActionItemOut(id="a", text="{{p:1}} will cook.", owner="{{p:1}} and {{p:3}}", derivedFrom=[cite])
            ],
            "decisions": [
                DecisionOut(
                    id="d",
                    text="Ask {{p:3}}.",
                    rationale="{{p:1}} said so",
                    alternatives=["{{p:10}}"],
                    derivedFrom=[cite],
                )
            ],
        }
    )
    plain = people.rendered_doc(doc, SPEAKERS)
    assert (plain.summary, plain.mom) == ("Joane and Noah host.", "### Party\n\nNoah invites Rabbi Ian.")
    assert (plain.actionItems[0].text, plain.actionItems[0].owner) == ("Joane will cook.", "Joane and Noah")
    decision = plain.decisions[0]
    assert (decision.text, decision.rationale, decision.alternatives) == ("Ask Noah.", "Joane said so", ["Speaker 10"])
    assert "{{" not in plain.model_dump_json()
    # The stored document keeps its tokens: a later rename shows everywhere.
    assert doc.summary == "{{p:1}} and {{p:3}} host."


def test_mom08_a_merge_moves_every_token_of_the_merged_speaker_to_the_one_kept() -> None:
    doc = MinutesOut.model_validate(
        {
            "summary": "{{p:7}} and {{p:3}} met; {{p:17}} too.",
            "openQuestions": [],
            "decisions": [],
            "mom": "{{p:7}}",
            "actionItems": [ActionItemOut(id="a", text="x", owner="{{p:7}}", derivedFrom=[])],
        }
    )
    moved = people.retold(doc, {7: 3})
    assert (moved.summary, moved.mom, moved.actionItems[0].owner) == (
        "{{p:3}} and {{p:3}} met; {{p:17}} too.",
        "{{p:3}}",
        "{{p:3}}",
    )


def test_mom06_given_the_lines_each_cite_names_its_line_s_speaker_as_they_are_now() -> None:
    from snapbak_backend.db.models import TranscriptLine

    said = TranscriptLine(id=uuid4(), recording_id=uuid4(), speaker_id=NOAH.id, idx=0, start_ms=0, end_ms=1, text="q")
    cite = DerivationOut(lineId=said.id, speaker="Speaker 3", atMs=0, quote="q")
    gone = DerivationOut(lineId=uuid4(), speaker="Old name", atMs=0, quote="q")
    doc = MinutesOut.model_validate(
        {
            "summary": "",
            "mom": "",
            "openQuestions": [],
            "decisions": [],
            "actionItems": [ActionItemOut(id="a", text="x", derivedFrom=[cite, gone])],
        }
    )
    plain = people.rendered_doc(doc, SPEAKERS, [said])
    assert [entry.speaker for entry in plain.actionItems[0].derivedFrom] == ["Noah", "Old name"]


def test_mom06_a_search_hit_s_owner_names_its_people_as_they_are_now() -> None:
    rows = [
        {"recording_id": "r1", "owner": "{{p:1}} and {{p:3}}", "kind": "action"},
        {"recording_id": "r2", "owner": None, "kind": "decision"},
    ]
    named = people.named_owners(rows, {"r1": SPEAKERS})
    assert [row["owner"] for row in named] == ["Joane and Noah", None]


def test_mom08_merges_in_a_chain_retell_a_token_to_the_speaker_kept_last() -> None:
    doc = MinutesOut.model_validate(
        {"summary": "{{p:7}} met {{p:4}}.", "openQuestions": [], "decisions": [], "mom": "", "actionItems": []}
    )
    # Round 1 merged 7 into 4, round 2 merged 4 into 2: Speaker 7 is Speaker 2 now.
    assert people.retold(doc, {7: 4, 4: 2}).summary == "{{p:2}} met {{p:2}}."


def test_mom06_a_malformed_token_never_reaches_the_words_and_feeds_the_correction() -> None:
    problems: list[str] = []
    made = people.checked("{{p:1}} met {{p:3} and {{ p:10 }} and {{p:}}.", SPEAKERS, problems, "mom")
    assert made == "{{p:1}} met  and  and ."
    assert problems == ["mom wrote {{p:3}, {{ p:10 }}, {{p:}}: a token is {{p:N}} or {{n:Name}}"]


def test_mom06_a_name_token_matches_a_speaker_s_name_whatever_its_case() -> None:
    assert people.checked("{{n:noah}} left.", SPEAKERS, [], "mom") == "{{p:3}} left."
