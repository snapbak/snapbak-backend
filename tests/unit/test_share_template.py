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

from snapbak_backend.api import minutes_page, share_page
from snapbak_backend.db.models import PageState, Share

APP_TOKENS = Path(__file__).parents[3] / "snapbak" / "web" / "tokens" / "generated" / "tokens.css"
OURS = Path(share_page.__file__).parent.parent / "templates" / "tokens.css"
BASE = "https://snapbak.site"
PAGE = f"{BASE}/v/Vb3kQx9_mT2LpZaR"
SUMMARY = "17 Oct 2026 · 1:01 · Shared with Snapbak"
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
    assert '<time datetime="2026-10-17T04:32:11Z">17 Oct 2026</time>' in page
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
    icons = [page.split('class="icon well">')[1].split("</svg>")[0] for page in pages]
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
    assert "--sb-color-accent: #8b6cff;" in page
    assert [str(attrs["href"])[:26] for tag, attrs in found if tag == "link"] == ["data:image/svg+xml;base64,"]
    assert [attrs for tag, attrs in found if tag == "script"] == [{}]


@pytest.mark.parametrize(("ms", "shown"), [(0, "0:00"), (61_200, "1:01"), (61_999, "1:01"), (3_723_000, "1:02:03")])
def test_shr01_the_duration_reads_like_a_player_clock(ms: int, shown: str) -> None:
    assert minutes_page.clock(ms) == shown


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
        ("p", {"class": "sub"}, "17 Oct 2026 · 1:01"),
        ("time", {"datetime": "2026-10-17T04:32:11Z"}, "17 Oct 2026"),
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
    assert f'<div class="icon well">{share_page.ICON.format(strokes)}</div>' in page
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
        '<div class="icon well"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" '
        f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{strokes}</svg></div>'
    ) in page


def test_shr01_the_date_is_the_utc_day_whatever_zone_the_time_comes_in() -> None:
    from datetime import timedelta, timezone

    late = share()
    late.created_at = datetime(2026, 10, 19, 3, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    page = share_page.render_page(late, BASE, VIDEO, POSTER, "https://snapbak.app")
    assert '<time datetime="2026-10-18T22:00:00Z">18 Oct 2026</time>' in page


def test_shr01_the_player_asks_for_a_fresh_url_at_most_once_a_minute() -> None:
    assert "if (Date.now() - refreshed < 60000) return;" in share_page.SCRIPT


def test_shr01_a_single_digit_day_reads_without_a_zero() -> None:
    early = share()
    early.created_at = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    page = share_page.render_page(early, BASE, VIDEO, POSTER, "https://snapbak.app")
    assert '<time datetime="2026-10-05T09:00:00Z">5 Oct 2026</time>' in page


AUDIO = "https://s3.example/audio/rec.m4a?X-Amz-Signature=c&X-Amz-Expires=3600"
LINE_ID = uuid.uuid4()


def minutes(
    mom: str = "## Decisions\n\nThe pricing page ships **Friday**.\n\n- Priya owns tiers\n- Amit checks",
) -> Any:
    from snapbak_backend.schemas.cloud import MinutesOut

    said = {"lineId": str(LINE_ID), "speaker": "Priya", "atMs": 812_000, "quote": "we ship Friday"}
    made = MinutesOut.model_validate(
        {
            "summary": "The team agreed the pricing page ships.",
            "mom": mom,
            "actionItems": [
                {"id": "act_1", "text": "Ship the tiers", "owner": "Priya", "due": "2026-10-30", "derivedFrom": [said]}
            ],
            "openQuestions": [{"id": "q_1", "text": "Does finance sign off?", "derivedFrom": [said]}],
            "decisions": [{"id": "dec_1", "text": "Ships Friday", "derivedFrom": [said]}],
            "pinned": [{"pinId": str(uuid.uuid4()), "atMs": 812_000, "note": "decided pricing"}],
        }
    )
    return made


def lines(count: int = 2) -> list[tuple[int, str, str]]:
    return [(n * 4_000, "Priya" if n % 2 else "Amit", f"Line {n} of the meeting, said plainly.") for n in range(count)]


def audio_page(sections: str = "", people: str = "") -> str:
    plain = share()
    plain.width = plain.height = None
    page = share_page.render_page(plain, BASE, AUDIO, None, "https://snapbak.app", "audio", sections, people)
    return page


def test_mom06_an_audio_share_plays_in_an_audio_element_with_no_video_tags() -> None:
    page = audio_page()
    found = parsed(page).tags
    audio = next(attrs for tag, attrs in found if tag == "audio")
    assert (audio["src"], audio["preload"]) == (AUDIO, "metadata")
    # The page's own player, as the mockup draws it (grading round 2): not the browser's controls.
    assert "controls" not in audio
    play = next(attrs for tag, attrs in found if tag == "button" and attrs.get("class") == "play")
    seek = next(attrs for tag, attrs in found if tag == "input")
    assert (play["aria-label"], play["type"]) == ("Play", "button")
    assert (seek["type"], seek["aria-label"], seek["max"]) == ("range", "Position", "61200")
    assert '<span class="time"><span data-now>00:00</span> / <span data-total>01:01</span></span>' in page
    assert not [tag for tag, _ in found if tag == "video"]
    assert not [name for name in metas(page) if name.startswith("og:video")]
    assert metas(page)["og:type"] == "website"
    assert len(page.encode()) < 100_000


def test_mom06_a_meeting_share_shows_the_minutes_and_the_timed_transcript() -> None:
    page = audio_page(share_page.meeting_sections(minutes(), lines()))
    assert "The team agreed the pricing page ships." in page
    assert "<h3>Decisions</h3><p>The pricing page ships <strong>Friday</strong>.</p>" in page
    assert "<ul><li>Priya owns tiers</li><li>Amit checks</li></ul>" in page
    # Dates and times as the app writes them (minutes.ts `due`, library/format.ts `clock`).
    assert "Ship the tiers" in page and "Priya · Due Fri 30 Oct" in page
    assert "Does finance sign off?" in page and "Ships Friday" in page
    assert '<a class="cite s2" href="#t=812"><i></i>Priya <b>13:32</b></a>' in page
    assert '<a class="at" href="#t=4">00:04</a>' in page
    order = [page.index(f"<h2>{name}</h2>") for name in ("Summary", "Minutes", "Action items", "Decisions")]
    later = [page.index(f"<h2>{name}</h2>") for name in ("Open questions", "Pins", "Transcript")]
    assert order + later == sorted(order + later)
    assert "decided pricing" in page and 'class="card plate tx"' in page
    # A speaker keeps one colour: Amit speaks first, so Priya is the second colour in the transcript and cites too.
    assert '<span class="speaker s1">Amit</span>' in share_page.meeting_sections(None, lines())
    assert "Line 1 of the meeting, said plainly." in page


def test_mom06_markup_in_the_minutes_or_a_line_stays_text() -> None:
    sections = share_page.meeting_sections(
        minutes("<script>alert(1)</script> **<img src=x onerror=alert(1)>**"),
        [(0, "<b>Mallory</b>", "<script>alert(2)</script>")],
    )
    assert "<script>alert" not in sections
    assert "<img" not in sections and "<b>Mallory" not in sections
    assert "<strong>&lt;img src=x onerror=alert(1)&gt;</strong>" in sections


def test_mom06_a_pin_without_a_note_reads_as_a_pinned_moment() -> None:
    bare = minutes().model_copy(update={"pinned": [minutes().pinned[0].model_copy(update={"note": None})]})
    assert "Pinned moment" in share_page.meeting_sections(bare, [])


def test_mom06_a_meeting_without_minutes_yet_shows_its_transcript() -> None:
    sections = share_page.meeting_sections(None, lines())
    assert "Line 0 of the meeting" in sections
    assert "Summary" not in sections


def test_mom06_the_meeting_page_frame_weighs_under_100_kb_and_a_two_hour_transcript_still_renders() -> None:
    frame = audio_page(share_page.meeting_sections(minutes(), []))
    assert len(frame.encode()) < 100_000, len(frame.encode())
    long = share_page.meeting_sections(minutes(), lines(1_800))
    assert long.count('href="#t=') == 1_800 + 4


def test_mom06_the_audio_download_is_an_m4a() -> None:
    assert share_page.attachment("Weekly sync", "m4a") == (
        "attachment; filename=\"Weekly sync.m4a\"; filename*=UTF-8''Weekly%20sync.m4a"
    )


def test_mom06_the_script_drives_either_player_and_seeks_from_a_timestamp() -> None:
    assert 'document.querySelector("video, audio")' in share_page.SCRIPT
    assert "#t=" in share_page.SCRIPT
    # The audio player's controls: play and pause, the lit level and the readout follow the element.
    for wired in ("button.play", "input[type=range]", "timeupdate", "data-now"):
        assert wired in share_page.SCRIPT


def test_mom06_a_meeting_page_names_what_its_download_gives() -> None:
    plain = share()
    video = share_page.render_page(plain, BASE, VIDEO, None, "https://snapbak.app", "video", "<p>minutes</p>")
    assert "Download video</a>" in video
    assert "Download audio</a>" in audio_page("<p>minutes</p>")
    assert "Download</a>" in render()


def test_mom06_only_a_dash_makes_a_list_as_the_app_s_renderer_reads_it() -> None:
    assert minutes_page.markdown("- one\n- two") == "<ul><li>one</li><li>two</li></ul>"
    assert minutes_page.markdown("* one") == "<p>* one</p>"


def test_mom06_a_due_that_is_no_date_is_shown_as_the_model_wrote_it() -> None:
    assert minutes_page.due("2026-10-31") == "Sat 31 Oct"
    assert minutes_page.due("next week") == "next week"


def test_mom06_a_meeting_page_names_its_speakers_under_the_title() -> None:
    page = audio_page(share_page.meeting_sections(minutes(), lines()), "Amit, Priya")
    assert "17 Oct 2026</time> · 1:01 · Amit, Priya</p>" in page
    assert share_page.speakers_in(lines()) == "Amit, Priya"


def full_minutes() -> Any:
    """Every section of minutes v2, each with one item."""
    from snapbak_backend.schemas.cloud import MinutesOut

    said = {"lineId": str(LINE_ID), "speaker": "Priya", "atMs": 812_000, "quote": "we ship Friday"}
    other = {**said, "atMs": 900_000, "speaker": "Amit"}
    decision = {"id": "dec_1", "text": "Ships Friday", "rationale": "billing is ready", "derivedFrom": [said]}
    made = MinutesOut.model_validate(
        {
            **minutes().model_dump(mode="json"),
            "title": "Pricing launch",
            "decisions": [{**decision, "alternatives": ["Monday", "<b>never</b>"]}],
            "risks": [
                {"id": "r_1", "kind": "blocker", "text": "Finance sign-off", "owner": "Amit", "derivedFrom": [said]}
            ],
            "followUps": [
                {"id": "f_1", "text": "Email support", "owner": None, "due": "Friday", "derivedFrom": [said]}
            ],
            "nextAgenda": [{"id": "n_1", "topic": "Annual plans", "derivedFrom": [said]}],
            "agenda": [{"id": "a_1", "topic": "Launch date", "status": "partial", "derivedFrom": [said]}],
            "contradictions": [{"id": "c_1", "text": "Friday or Monday", "derivedFrom": [said, other]}],
        }
    )
    return made


def test_mom05_a_meeting_share_shows_every_section_of_the_minutes() -> None:
    page = audio_page(share_page.meeting_sections(full_minutes(), lines()))
    assert "Launch date" in page and "Partly covered" in page
    assert "Why: billing is ready" in page and "Not chosen: Monday; &lt;b&gt;never&lt;/b&gt;" in page
    assert "<b>Blocker</b> Finance sign-off" in page and "Amit" in page
    assert "Email support" in page and "No owner · Due Friday" in page
    assert "Annual plans" in page
    assert page.count('href="#t=900"') == 1  # the contradiction cites both of its lines
    assert len(page.encode()) < 100_000


def test_mom05_empty_sections_of_the_minutes_are_left_out() -> None:
    sections = share_page.meeting_sections(minutes(), [])
    for name in ("Agenda coverage", "Follow-ups", "Risks and blockers", "Contradictions", "Next meeting"):
        assert f"<h2>{name}</h2>" not in sections


def test_mom06_the_share_page_orders_the_sections_as_the_app_does() -> None:
    """The app's order (web/studio sections.ts, MEETINGS.md 6.2) and its headings (grading 3)."""
    page = audio_page(share_page.meeting_sections(full_minutes(), lines()))
    names = ["Summary", "Minutes", "Action items", "Decisions", "Open questions", "Risks and blockers"]
    names += ["Follow-ups", "Next meeting", "Agenda coverage", "Contradictions", "Pins", "Transcript"]
    found = [page.index(f"<h2>{name}</h2>") for name in names]
    assert found == sorted(found)


def test_mom06_the_meeting_waveform_is_the_shape_of_its_speech() -> None:
    """The mockup's audio player is a waveform, not a bare track (grading 3). With no audio peaks on the server,
    each bar is how much of its slice someone was speaking: talk in the first half, silence in the second."""
    said = [(n * 1_000, n * 1_000 + 900) for n in range(30)]
    bars = share_page.speech_bars(said, 60_000)
    assert len(bars) == share_page.BARS
    half = share_page.BARS // 2
    assert min(bars[: half - 1]) > max(bars[half + 1 :])
    assert all(share_page.QUIETEST <= bar <= 1.0 for bar in bars)
    assert share_page.speech_bars([], 60_000) == [] and share_page.speech_bars(said, 0) == []


def test_mom06_an_audio_page_draws_the_waveform_twice_its_lit_copy_follows_the_time() -> None:
    bars = share_page.speech_bars([(0, 4_000), (30_000, 33_000)], 61_200)
    plain = share()
    plain.width = plain.height = None
    page = share_page.render_page(plain, BASE, AUDIO, None, "https://snapbak.app", "audio", "", "", bars)
    assert page.count('<svg class="wave"') == 2 and page.count("<rect ") == 2 * share_page.BARS
    assert '<span class="level"><svg class="wave"' in page
    assert len(page.encode()) < 100_000
    assert '<svg class="wave"' not in audio_page()


def test_mom06_on_a_phone_the_waveform_draws_every_other_bar() -> None:
    """At 390 px 96 bars blurred into one smear (grading 4): under 480 px every other bar is left out."""
    page = share_page.SHARE.template
    assert "@media (max-width: 480px)" in page and ".wave rect:nth-child(2n)" in page
