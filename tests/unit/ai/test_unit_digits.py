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
        ("meri teen saal ki beti", "Meri teen saal ki beti.", "Meri 3 saal ki beti."),
        ("उन्हें पांच दिन आराम करना है", "उन्हें पांच दिन आराम करना है।", "उन्हें 5 दिन आराम करना है।"),
    ],
)
def test_a_number_word_before_a_unit_becomes_digits(said: str, wrote: str, mended: str) -> None:
    assert shaped(said, wrote) == mended


@pytest.mark.parametrize(
    ("said", "wrote"),
    [
        # A rough pair ("a couple of days"), joined or hyphenated.
        ("do chaar din me ho jayega", "Do chaar din me ho jayega."),
        ("do-chaar din ka delay chalega", "Do-chaar din ka delay chalega."),
        # A fraction word goes with the number after it.
        ("साढ़े तीन किलो चावल चाहिए", "साढ़े तीन किलो चावल चाहिए।"),
        # A sentence that opens with the number keeps the word.
        ("two days ago she told me", "Two days ago she told me."),
        # "do" and "दो" are also the verb "give".
        ("mujhe do din do", "Mujhe do din do."),
        ("मैंने दो किलो लिए", "मैंने दो किलो लिए।"),
        # A count of things, not a unit, stays a word.
        ("we have three problems", "We have three problems."),
    ],
)
def test_a_number_word_that_is_not_a_measure_stays_a_word(said: str, wrote: str) -> None:
    assert shaped(said, wrote) == wrote
