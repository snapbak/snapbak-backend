"""Share slugs: 96 random bits as 16 URL-safe characters, and the page route's pattern (12 LLD, Slugs)."""

import string

import pytest

from snapbak_backend.services.shares import is_slug, new_slug

URL_SAFE = set(string.ascii_letters + string.digits + "-_")


def test_shr01_a_slug_is_16_url_safe_characters_and_never_repeats() -> None:
    made = {new_slug() for _ in range(1000)}
    assert len(made) == 1000
    assert {len(slug) for slug in made} == {16}
    assert set().union(*made) <= URL_SAFE


@pytest.mark.parametrize("slug", ["Vb3kQx9_mT2LpZaR", "a" * 64, "-_" * 8])
def test_shr01_the_page_takes_16_to_64_url_safe_characters(slug: str) -> None:
    assert is_slug(slug)


@pytest.mark.parametrize(
    "slug", ["Vb3kQx9_mT2LpZa", "a" * 65, "../etc/passwd/xxxx", "Vb3kQx9_mT2LpZaR.", "Vb3kQx9 mT2LpZaR", ""]
)
def test_shr01_the_page_refuses_anything_else(slug: str) -> None:
    assert not is_slug(slug)
