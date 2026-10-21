"""The share page and its error page as rendered HTML: the Open Graph tags, `noindex`, escaping, the CSP hash of the
inline script, the weight, and the app's design tokens (SHR-01, 12 LLD, Template)."""

import base64
import hashlib
import uuid
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar

import pytest

from snapbak_backend.api import share_page
from snapbak_backend.db.models import PageState, Share

APP_TOKENS = Path(__file__).parents[3] / "snapbak" / "web" / "tokens" / "generated" / "tokens.css"
OURS = Path(share_page.__file__).parent.parent / "templates" / "tokens.css"
BASE = "https://snapbak.site"
PAGE = f"{BASE}/v/Vb3kQx9_mT2LpZaR"
SUMMARY = "Oct 17, 2026 · 1:01 · Shared with Snapbak"
VIDEO = "https://s3.example/shares/v1/video.mp4?X-Amz-Signature=a&X-Amz-Expires=3600"
POSTER = "https://s3.example/shares/v1/poster.jpg?X-Amz-Signature=b&X-Amz-Expires=3600"


class Tags(HTMLParser):
    """Every start tag's attributes, and the text of `<script>`."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.script = ""
        self.inside = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        self.inside = tag

    def handle_endtag(self, tag: str) -> None:
        self.inside = ""

    def handle_data(self, data: str) -> None:
        if self.inside == "script":
            self.script += data


def parsed(page: str) -> Tags:
    tags = Tags()
    tags.feed(page)
    return tags


def metas(page: str) -> dict[str, str | None]:
    found = parsed(page).tags
    named = {attrs.get("property") or attrs.get("name"): attrs.get("content") for tag, attrs in found if tag == "meta"}
    return {str(name): content for name, content in named.items() if name}


def share(title: str = "Recording 2026-10-17 at 10.02.11") -> Share:
    return Share(
        id=uuid.uuid4(),
        slug="Vb3kQx9_mT2LpZaR",
        title=title,
        width=1920,
        height=1080,
        duration_ms=61_200,
        created_at=datetime(2026, 10, 17, 4, 32, 11, tzinfo=UTC),
    )


def render(title: str = "Recording 2026-10-17 at 10.02.11", poster: str | None = POSTER) -> str:
    page = share_page.render_page(share(title), BASE, VIDEO, poster, "https://snapbak.app")
    return page


def test_shr01_the_page_has_every_open_graph_and_twitter_tag_and_noindex() -> None:
    tags = metas(render())
    assert {name: value for name, value in tags.items() if name not in ("viewport",)} == {
        "robots": "noindex, nofollow",
        "description": SUMMARY,
        "og:type": "video.other",
        "og:site_name": "Snapbak",
        "og:title": "Recording 2026-10-17 at 10.02.11",
        "og:description": SUMMARY,
        "og:url": PAGE,
        "og:image": f"{PAGE}/poster.jpg",
        "og:image:alt": "Recording 2026-10-17 at 10.02.11",
        "og:video": f"{PAGE}/video.mp4",
        "og:video:secure_url": f"{PAGE}/video.mp4",
        "og:video:type": "video/mp4",
        "og:video:width": "1920",
        "og:video:height": "1080",
        "twitter:card": "summary_large_image",
        "twitter:title": "Recording 2026-10-17 at 10.02.11",
        "twitter:image": f"{PAGE}/poster.jpg",
    }


def test_shr01_the_page_plays_the_signed_video_with_its_poster_and_links_the_download() -> None:
    page = render()
    found = parsed(page).tags
    video = next(attrs for tag, attrs in found if tag == "video")
    links = [attrs.get("href") for tag, attrs in found if tag == "a"]
    assert (video["src"], video["poster"], video["width"], video["height"]) == (VIDEO, POSTER, "1920", "1080")
    assert {"controls", "playsinline"} <= set(video)
    assert video["preload"] == "metadata"
    assert f"{PAGE}/download" in links
    assert "https://snapbak.app?ref=share" in links
    assert '<time datetime="2026-10-17T04:32:11Z">Oct 17, 2026</time>' in page
    assert "<title>Recording 2026-10-17 at 10.02.11 · Snapbak</title>" in page
    assert '<body style="--w: 1920; --h: 1080">' in page
    assert ' poster="https://s3.example/shares/v1/poster.jpg?X-Amz-Signature=b&amp;X-Amz-Expires=3600"' in page
    assert page.startswith('<!doctype html>\n<html lang="en">')
    assert f'<link rel="icon" href="{share_page.STATIC["icon"]}">' in page


def test_shr01_without_a_poster_the_unfurl_shows_the_branded_card_and_the_player_no_poster_frame() -> None:
    page = render(poster=None)
    tags = metas(page)
    video = next(attrs for tag, attrs in parsed(page).tags if tag == "video")
    images = {name: value for name, value in tags.items() if "image" in name or name == "twitter:card"}
    assert images == {
        "og:image": f"{BASE}/og.jpg",
        "og:image:width": "1200",
        "og:image:height": "630",
        "og:image:alt": "Snapbak",
        "twitter:card": "summary_large_image",
        "twitter:image": f"{BASE}/og.jpg",
    }
    assert "poster" not in video


def test_shr01_the_branded_card_is_a_1200_by_630_jpeg_under_50_kb() -> None:
    card = share_page.OG_CARD
    frame = card.index(b"\xff\xc0")
    height, width = int.from_bytes(card[frame + 5 : frame + 7]), int.from_bytes(card[frame + 7 : frame + 9])
    assert (card[:2], width, height) == (b"\xff\xd8", 1200, 630)
    assert len(card) < 50_000, len(card)


def test_shr01_the_date_is_rendered_once_on_the_server_never_swapped_by_the_script() -> None:
    script = parsed(render()).script
    assert "Intl" not in script
    assert "textContent" not in script


def test_shr01_a_title_with_markup_stays_text() -> None:
    title = '</title><script>alert("x")</script> & <b>'
    page = render(title)
    assert "<script>alert" not in page
    assert "<b>" not in page
    assert metas(page)["og:title"] == title
    assert "&lt;/title&gt;&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &lt;b&gt;" in page


def test_shr01_the_csp_names_the_hash_of_the_one_inline_script_and_the_media_origin() -> None:
    script = parsed(render()).script
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    policy = share_page.page_policy("https://s3.example")
    assert script.strip()
    assert f"script-src 'sha256-{digest}'" in policy
    assert "media-src https://s3.example;" in policy
    assert "img-src data: https://s3.example;" in policy
    assert policy.startswith("default-src 'none';")


def test_shr01_the_page_weighs_under_the_100_kb_cap_before_the_video() -> None:
    weight = len(render().encode())
    assert weight < 100_000, weight


@pytest.mark.parametrize(
    ("state", "status", "message"),
    [
        ("uploading", 404, "This video isn't ready yet"),
        ("disabled", 410, "This link was turned off"),
        ("expired", 410, "This link has expired"),
        ("gone", 404, "This video isn't available"),
    ],
)
def test_shr01_each_state_but_live_has_its_error_page(state: PageState, status: int, message: str) -> None:
    page, code = share_page.render_error(state, "https://snapbak.app")
    assert code == status
    assert f"<h1>{message.replace("'", '&#x27;')}</h1>" in page
    assert metas(page)["robots"] == "noindex, nofollow"
    assert "<script" not in page
    assert "<video" not in page
    assert ('<meta http-equiv="refresh" content="30">' in page) == (state == "uploading")


def test_shr01_each_error_state_has_its_own_icon() -> None:
    states: list[PageState] = ["uploading", "disabled", "expired", "gone"]
    pages = [share_page.render_error(state, "https://snapbak.app")[0] for state in states]
    icons = [page.split('class="icon">')[1].split("</svg>")[0] for page in pages]
    assert len(set(icons)) == 4


def test_shr01_the_download_is_an_attachment_named_after_the_title() -> None:
    assert share_page.attachment("Recording 2026-10-17 at 10.02.11") == (
        'attachment; filename="Recording 2026-10-17 at 10.02.11.mp4"; '
        "filename*=UTF-8''Recording%202026-10-17%20at%2010.02.11.mp4"
    )


def test_shr01_a_download_name_keeps_no_quote_or_line_break_and_keeps_unicode_in_the_star_form() -> None:
    said = share_page.attachment('Démo "v2"\r\nX-Evil: 1')
    assert "\r" not in said
    assert "\n" not in said
    assert said == (
        'attachment; filename="D_mo _v2___X-Evil_ 1.mp4"; '
        "filename*=UTF-8''D%C3%A9mo%20%22v2%22%0D%0AX-Evil%3A%201.mp4"
    )


@pytest.mark.skipif(not APP_TOKENS.is_file(), reason="the app repo is not checked out next to this one")
def test_shr01_the_page_tokens_are_a_byte_identical_copy_of_the_apps() -> None:
    assert OURS.read_bytes() == APP_TOKENS.read_bytes(), f"copy {APP_TOKENS} to {OURS}"


def test_shr01_the_page_inlines_its_tokens_and_icon_and_loads_no_script_or_stylesheet() -> None:
    page = render()
    found = parsed(page).tags
    assert "--sb-color-accent: #8b5cf6;" in page
    assert [str(attrs["href"])[:26] for tag, attrs in found if tag == "link"] == ["data:image/svg+xml;base64,"]
    assert [attrs for tag, attrs in found if tag == "script"] == [{}]


@pytest.mark.parametrize(("ms", "shown"), [(0, "0:00"), (61_200, "1:01"), (61_999, "1:01"), (3_723_000, "1:02:03")])
def test_shr01_the_duration_reads_like_a_player_clock(ms: int, shown: str) -> None:
    assert share_page.clock(ms) == shown


class Golden(HTMLParser):
    """The elements a viewer or a link preview reads, as (tag, chosen attributes, text) in page order."""

    KEEP: ClassVar[dict[str, tuple[str, ...]]] = {
        "title": (),
        "meta": ("charset", "name", "property", "http-equiv", "content"),
        "link": ("rel",),
        "a": ("class", "href"),
        "video": ("src", "poster", "width", "height", "controls", "playsinline", "preload"),
        "h1": ("title",),
        "p": ("class",),
        "time": ("datetime",),
        "footer": (),
        "body": ("style",),
    }

    def __init__(self) -> None:
        super().__init__()
        self.found: list[list[Any]] = []
        self.open: list[list[Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.KEEP:
            kept = {name: value for name, value in attrs if name in self.KEEP[tag]}
            entry = [tag, kept, ""]
            self.found.append(entry)
            if tag not in ("meta", "link", "video", "body"):
                self.open.append(entry)

    def handle_endtag(self, tag: str) -> None:
        if self.open and self.open[-1][0] == tag:
            self.open.pop()

    def handle_data(self, data: str) -> None:
        for entry in self.open:
            entry[2] += data


def golden(page: str) -> list[tuple[str, dict[str, str | None], str]]:
    parser = Golden()
    parser.feed(page)
    elements = [(tag, attrs, " ".join(text.split())) for tag, attrs, text in parser.found]
    return elements


TITLE = "Recording 2026-10-17 at 10.02.11"


def test_shr01_the_live_page_golden() -> None:
    elements = [entry for entry in golden(render()) if entry[0] != "link"]
    assert elements == [
        ("meta", {"charset": "utf-8"}, ""),
        ("meta", {"name": "viewport", "content": "width=device-width, initial-scale=1"}, ""),
        ("meta", {"name": "robots", "content": "noindex, nofollow"}, ""),
        ("title", {}, f"{TITLE} · Snapbak"),
        ("meta", {"name": "description", "content": SUMMARY}, ""),
        ("meta", {"property": "og:type", "content": "video.other"}, ""),
        ("meta", {"property": "og:site_name", "content": "Snapbak"}, ""),
        ("meta", {"property": "og:title", "content": TITLE}, ""),
        ("meta", {"property": "og:description", "content": SUMMARY}, ""),
        ("meta", {"property": "og:url", "content": PAGE}, ""),
        ("meta", {"property": "og:image", "content": f"{PAGE}/poster.jpg"}, ""),
        ("meta", {"property": "og:image:alt", "content": TITLE}, ""),
        ("meta", {"name": "twitter:image", "content": f"{PAGE}/poster.jpg"}, ""),
        ("meta", {"property": "og:video", "content": f"{PAGE}/video.mp4"}, ""),
        ("meta", {"property": "og:video:secure_url", "content": f"{PAGE}/video.mp4"}, ""),
        ("meta", {"property": "og:video:type", "content": "video/mp4"}, ""),
        ("meta", {"property": "og:video:width", "content": "1920"}, ""),
        ("meta", {"property": "og:video:height", "content": "1080"}, ""),
        ("meta", {"name": "twitter:card", "content": "summary_large_image"}, ""),
        ("meta", {"name": "twitter:title", "content": TITLE}, ""),
        ("body", {"style": "--w: 1920; --h: 1080"}, ""),
        ("a", {"class": "brand", "href": "https://snapbak.app?ref=share"}, "Snapbak"),
        (
            "video",
            {
                "controls": None,
                "playsinline": None,
                "preload": "metadata",
                "poster": POSTER,
                "width": "1920",
                "height": "1080",
                "src": VIDEO,
            },
            "",
        ),
        ("h1", {"title": TITLE}, TITLE),
        ("p", {"class": "sub"}, "Oct 17, 2026 · 1:01"),
        ("time", {"datetime": "2026-10-17T04:32:11Z"}, "Oct 17, 2026"),
        ("a", {"class": "button", "href": f"{PAGE}/download"}, "Download"),
        ("footer", {}, "Made with Snapbak · Get Snapbak →"),
        ("a", {"href": "https://snapbak.app?ref=share"}, "Made with Snapbak · Get Snapbak →"),
    ]


@pytest.mark.parametrize(
    ("state", "message", "hint"),
    [
        ("uploading", "This video isn't ready yet", "It's still uploading. This page checks again on its own."),
        ("disabled", "This link was turned off", "The person who shared it turned it off."),
        ("expired", "This link has expired", "Ask the person who shared it for a new link."),
        ("gone", "This video isn't available", "It was deleted, or the link is mistyped."),
    ],
)
def test_shr01_each_error_page_golden(state: PageState, message: str, hint: str) -> None:
    page, _ = share_page.render_error(state, "https://snapbak.app")
    elements = [entry for entry in golden(page) if entry[0] != "link"]
    refresh = [("meta", {"http-equiv": "refresh", "content": "30"}, "")] if state == "uploading" else []
    assert elements == [
        ("meta", {"charset": "utf-8"}, ""),
        ("meta", {"name": "viewport", "content": "width=device-width, initial-scale=1"}, ""),
        ("meta", {"name": "robots", "content": "noindex, nofollow"}, ""),
        *refresh,
        ("title", {}, f"{message} · Snapbak"),
        ("meta", {"property": "og:site_name", "content": "Snapbak"}, ""),
        ("meta", {"property": "og:title", "content": message}, ""),
        ("body", {}, ""),
        ("a", {"class": "brand", "href": "https://snapbak.app?ref=share"}, "Snapbak"),
        ("h1", {}, message),
        ("p", {}, hint),
        ("a", {"class": "button", "href": "https://snapbak.app?ref=share"}, "Get Snapbak"),
        ("footer", {}, "Snapbak records your screen, then edits and shares it in a click."),
    ]
    strokes = share_page.ERRORS[state][3]
    assert f'<div class="icon">{share_page.ICON.format(strokes)}</div>' in page
    assert 'stroke-width="1.8"' in share_page.ICON
    assert page.startswith('<!doctype html>\n<html lang="en">')
    assert f'<link rel="icon" href="{share_page.STATIC["icon"]}">' in page


def test_shr01_a_slash_in_the_title_is_escaped_in_the_download_name() -> None:
    assert share_page.attachment("Q3/Q4 plan") == (
        "attachment; filename=\"Q3_Q4 plan.mp4\"; filename*=UTF-8''Q3%2FQ4%20plan.mp4"
    )


@pytest.mark.parametrize(
    ("state", "strokes"),
    [
        ("uploading", '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
        (
            "disabled",
            '<path d="M9 17H7a5 5 0 0 1 0-10h2"/><path d="M15 7h2a5 5 0 0 1 3.5 8.5"/><path d="M8 12h4"/>'
            '<path d="M3 3l18 18"/>',
        ),
        (
            "expired",
            '<path d="M7 3h10M7 21h10"/><path d="M8 3c0 5 8 5 8 9s-8 4-8 9"/><path d="M16 3c0 5-8 5-8 9s8 4 8 9"/>',
        ),
        (
            "gone",
            '<rect x="2.5" y="6" width="13" height="12" rx="2.5"/><path d="M15.5 10.5 21 7.5v9l-5.5-3"/>'
            '<path d="M3 3l18 18"/>',
        ),
    ],
)
def test_shr01_each_error_page_draws_its_own_literal_icon(state: PageState, strokes: str) -> None:
    page, _ = share_page.render_error(state, "https://snapbak.app")
    assert (
        '<div class="icon"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" '
        f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{strokes}</svg></div>'
    ) in page


def test_shr01_the_date_is_the_utc_day_whatever_zone_the_time_comes_in() -> None:
    from datetime import timedelta, timezone

    late = share()
    late.created_at = datetime(2026, 10, 19, 3, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    page = share_page.render_page(late, BASE, VIDEO, POSTER, "https://snapbak.app")
    assert '<time datetime="2026-10-18T22:00:00Z">Oct 18, 2026</time>' in page


def test_shr01_the_player_asks_for_a_fresh_url_at_most_once_a_minute() -> None:
    assert "if (Date.now() - refreshed < 60000) return;" in share_page.SCRIPT


def test_shr01_a_single_digit_day_reads_without_a_zero() -> None:
    early = share()
    early.created_at = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    page = share_page.render_page(early, BASE, VIDEO, POSTER, "https://snapbak.app")
    assert '<time datetime="2026-10-05T09:00:00Z">Oct 5, 2026</time>' in page
