"""A number word before a unit of time, money or measure is written as digits (prompt rule 4), without a model."""

import pytest

from snapbak_backend.ai.guardrail.shape import shaped


@pytest.mark.parametrize(
    ("said", "wrote", "mended"),
    [
        ("we can review this in two hours", "We can review this in two hours.", "We can review this in 2 hours."),
        (
            "they need my six slips two years of returns",
            "They need my six slips, two years of returns.",
            "They need my six slips, 2 years of returns.",
        ),
    ],
)
def test_a_number_word_before_a_unit_becomes_digits(said: str, wrote: str, mended: str) -> None:
    assert shaped(said, wrote) == mended


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # A rough pair ("two or three days").
        ("it takes two three days", "It takes two three days."),
        # A sentence that opens with the number keeps the word.
        ("two days ago she told me", "Two days ago she told me."),
        # A count of things, not a unit, stays a word.
        ("we have three problems", "We have three problems."),
    ],
)
def test_a_number_word_that_is_not_a_measure_stays_a_word(said: str, wrote: str) -> None:
    assert shaped(said, wrote) == wrote
