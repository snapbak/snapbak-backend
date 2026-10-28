"""The public share page `snapbak.site/v/{slug}` and its helper routes (SHR-01, D18; 12 LLD, Public page routes). No
sign-in: the slug is the secret. Every answer checks the share's state, and every video URL is signed per request.

The templates are stdlib `string.Template`s: each value is HTML-escaped here, and only the fixed pieces (tokens, CSS,
mark, script) and fragments built from escaped values go in raw."""

import base64
import hashlib
import html
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from string import Template
from typing import Literal
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select

from snapbak_backend.api import cloud_deps
from snapbak_backend.api.cloud_deps import Session
from snapbak_backend.api.logged import LoggedRoute
from snapbak_backend.api.minutes_page import Said, clock, meeting_sections, stamp
from snapbak_backend.db.models import PageState, Recording, Share, Upload, page_state
from snapbak_backend.schemas.cloud import UTC_FORMAT
from snapbak_backend.services import meetings, people, recordings
from snapbak_backend.services.recordings import speaker_of
from snapbak_backend.services.shares import SIGNED_S, is_slug, link

router = APIRouter(route_class=LoggedRoute)

TEMPLATES = Path(__file__).parent.parent / "templates"
SHARE = Template((TEMPLATES / "share.html").read_text())
ERROR = Template((TEMPLATES / "error.html").read_text())
SCRIPT = (TEMPLATES / "share.js").read_text()
MARK = (TEMPLATES / "mark.svg").read_text().strip()
STATIC = {
    "tokens": (TEMPLATES / "tokens.css").read_text(),
    "style": (TEMPLATES / "page.css").read_text(),
    "mark": MARK,
    "icon": "data:image/svg+xml;base64," + base64.b64encode(MARK.encode()).decode(),
}
SCRIPT_HASH = "sha256-" + base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
# The card a link preview shows when the share has no poster (12 HLD, The page).
OG_CARD = (TEMPLATES / "og.jpg").read_bytes()
OG_CARD_TAGS = Template(
    '<meta property="og:image" content="$base/og.jpg">\n<meta property="og:image:width" content="1200">\n'
    '<meta property="og:image:height" content="630">\n<meta property="og:image:alt" content="Snapbak">\n'
    '<meta name="twitter:image" content="$base/og.jpg">\n'
)
POSTER_TAGS = Template(
    '<meta property="og:image" content="$page_url/poster.jpg">\n<meta property="og:image:alt" content="$title">\n'
    '<meta name="twitter:image" content="$page_url/poster.jpg">\n'
)
# Each icon's strokes, drawn in a 24-unit box like the app's line icons.
CLOCK = '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'
LINK_OFF = (
    '<path d="M9 17H7a5 5 0 0 1 0-10h2"/><path d="M15 7h2a5 5 0 0 1 3.5 8.5"/><path d="M8 12h4"/><path d="M3 3l18 18"/>'
)
HOURGLASS = '<path d="M7 3h10M7 21h10"/><path d="M8 3c0 5 8 5 8 9s-8 4-8 9"/><path d="M16 3c0 5-8 5-8 9s8 4 8 9"/>'
CAMERA_OFF = (
    '<rect x="2.5" y="6" width="13" height="12" rx="2.5"/><path d="M15.5 10.5 21 7.5v9l-5.5-3"/><path d="M3 3l18 18"/>'
)
ERRORS: dict[PageState, tuple[int, str, str, str]] = {
    "uploading": (404, "This video isn't ready yet", "It's still uploading. This page checks again on its own.", CLOCK),
    "disabled": (410, "This link was turned off", "The person who shared it turned it off.", LINK_OFF),
    "expired": (410, "This link has expired", "Ask the person who shared it for a new link.", HOURGLASS),
    "gone": (404, "This video isn't available", "It was deleted, or the link is mistyped.", CAMERA_OFF),
}
ICON = (
    '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{}</svg>'
)
# The not-ready page looks again every 30 s, so a viewer who opened it early sees the video once it is up.
REFRESH = '<meta http-equiv="refresh" content="30">\n'
ERROR_POLICY = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
ROBOTS = "User-agent: *\nDisallow: /v/\n"
NO_STORE = {"Cache-Control": "no-store"}
# Link preview bots may ask HEAD first.
READ = ["GET", "HEAD"]


def fill(template: Template, raw: Mapping[str, str], **text: str) -> str:
    escaped = {name: html.escape(value) for name, value in text.items()}
    page = template.substitute({**STATIC, **raw, **escaped})
    return page


def page_policy(media_origin: str) -> str:
    """Only the one inline script by its hash, styles inline, the video and poster from the bucket, and /src."""
    return (
        f"default-src 'none'; img-src data: {media_origin}; media-src {media_origin}; style-src 'unsafe-inline'; "
        f"script-src '{SCRIPT_HASH}'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    )


def page_headers(policy: str) -> dict[str, str]:
    """The body holds a signed URL, so nothing caches it, and the slug never leaves in a referrer."""
    return {
        "Cache-Control": "private, no-store",
        "X-Robots-Tag": "noindex, nofollow",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": policy,
        "Strict-Transport-Security": "max-age=31536000",
    }


def speakers_in(lines: list[Said]) -> str:
    """The meeting's speakers in the order they first spoke, for the page's subtitle."""
    return ", ".join(dict.fromkeys(who for _, who, _ in lines if who))


VIDEO_TAGS = Template(
    '<meta property="og:video" content="$page_url/video.mp4">\n'
    '<meta property="og:video:secure_url" content="$page_url/video.mp4">\n'
    '<meta property="og:video:type" content="video/mp4">\n<meta property="og:video:width" content="$width">\n'
    '<meta property="og:video:height" content="$height">\n'
)
VIDEO_PLAYER = Template(
    '<div class="player plate"><video controls playsinline preload="metadata"$poster_attr width="$width" '
    'height="$height" src="$url"></video></div>'
)
# A GIF page plays the GIF itself in an img, and its unfurl animates the GIF too, so og:image names the
# animation route (GIFS-AND-REPLAY-60, G7): Slack, X and LinkedIn animate an image/gif og:image.
GIF_TAGS = Template(
    '<meta property="og:image" content="$page_url/animation.gif">\n'
    '<meta property="og:image:type" content="image/gif">\n'
    '<meta property="og:image:width" content="$width">\n'
    '<meta property="og:image:height" content="$height">\n'
    '<meta property="og:image:alt" content="$title">\n'
    '<meta name="twitter:image" content="$page_url/animation.gif">\n'
)
GIF_PLAYER = Template('<div class="player plate"><img src="$url" width="$width" height="$height" alt="$title"></div>')
# The audio page's own player: a round play button, a well whose lit level is the time played (a range input over
# it seeks, keys included), and the time; share.js drives it from the element. A meeting's well holds its waveform
# (`speech_bars`), drawn twice: muted, and lit inside the level, so the played part lights up as the mockup draws
# it. An audio share with no transcript has no waveform to draw: its level is a bare bar.
AUDIO_PLAYER = Template(
    '<div class="player plate audio"><button class="play" type="button" aria-label="Play">'
    '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path class="on" d="M8 5v14l11-7z"/>'
    '<path class="off" d="M7 5h4v14H7zM13 5h4v14h-4z"/></svg></button>'
    '<div class="track well$waved">$wave<span class="level">$wave</span>'
    '<input type="range" aria-label="Position" min="0" max="$max" step="1000" value="0"></div>'
    '<span class="time"><span data-now>00:00</span> / <span data-total>$total</span></span>'
    '<audio preload="metadata" src="$url"></audio></div>'
)
# The waveform's bars, and the quietest one's height: a pause still shows as a low bar, as the mockup draws it.
BARS = 96
QUIETEST = 0.15


def speech_bars(spans: list[tuple[int, int]], duration_ms: int) -> list[float]:
    """Each bar's height, 0 to 1: the share of its slice of the meeting that someone was speaking, from the
    transcript's lines (the server holds no audio peaks), scaled so the busiest bar is full and a silent one is
    QUIETEST. No lines or no length draw no waveform."""
    if not spans or duration_ms <= 0:
        return []
    width = duration_ms / BARS
    spoken = [0.0] * BARS
    for start, end in spans:
        first, last = int(start // width), min(int(end // width), BARS - 1)
        for n in range(max(first, 0), last + 1):
            spoken[n] += max(0.0, min(end, (n + 1) * width) - max(start, n * width))
    low, high = min(spoken), max(spoken)
    if high <= low:
        return [1.0] * BARS
    return [round(QUIETEST + (1 - QUIETEST) * (said - low) / (high - low), 3) for said in spoken]


def waveform(bars: list[float]) -> str:
    """The bars as one inline SVG (attributes only, so the page's CSP needs nothing more), stretched to the well."""
    rects = "".join(
        f'<rect x="{n + 0.2}" y="{round(50 - 50 * bar, 1)}" width="0.6" height="{round(100 * bar, 1)}" rx="0.3"/>'
        for n, bar in enumerate(bars)
    )
    made = (
        f'<svg class="wave" viewBox="0 0 {len(bars)} 100" preserveAspectRatio="none" aria-hidden="true">{rects}</svg>'
    )
    return made


type Media = Literal["video", "audio", "gif"]
EXTENSIONS: dict[Media, str] = {"video": "mp4", "audio": "m4a", "gif": "gif"}


def render_page(
    share: Share,
    base: str,
    media_url: str,
    poster_url: str | None,
    home_url: str,
    media: Media = "video",
    sections: str = "",
    people: str = "",
    bars: list[float] | None = None,
) -> str:
    """The page for a live share: the player, and a meeting's `sections` below it. The date is the UTC day the share
    was made, the same text in the unfurl and on the page. An audio page has no frame size, so its body takes 16:9
    only for the column's width."""
    page_url = link(base, share.slug)
    created = share.created_at.astimezone(UTC)
    day = f"{created.day} {created:%b} {created.year}"
    duration = clock(share.duration_ms or 0)
    if media == "gif":
        duration = size_line(share.video_bytes)
    width, height = str(share.width or 16), str(share.height or 9)
    if poster_url is None:
        image_tags = fill(OG_CARD_TAGS, {}, base=base)
        poster_attr = ""
    else:
        image_tags = fill(POSTER_TAGS, {}, page_url=page_url, title=share.title)
        poster_attr = f' poster="{html.escape(poster_url)}"'
    if media == "gif":
        image_tags = fill(GIF_TAGS, {}, page_url=page_url, title=share.title, width=width, height=height)
        poster_attr = ""
        video_tags = ""
        player = fill(GIF_PLAYER, {}, url=media_url, width=width, height=height, title=share.title)
    elif media == "video":
        video_tags = fill(VIDEO_TAGS, {}, page_url=page_url, width=width, height=height)
        player = fill(VIDEO_PLAYER, {"poster_attr": poster_attr}, url=media_url, width=width, height=height)
    else:
        video_tags = ""
        wave = waveform(bars) if bars else ""
        trusted = {"wave": wave, "waved": " waved" if wave else ""}
        length = share.duration_ms or 0
        player = fill(AUDIO_PLAYER, trusted, url=media_url, max=str(length), total=stamp(length))
    label = "Download GIF" if media == "gif" else f"Download {media}"
    download = "Download" if media == "video" and not sections else label
    raw = {
        "image_tags": image_tags,
        "video_tags": video_tags,
        "player": player,
        "sections": sections,
        "body_class": ' class="meeting"' if sections else "",
        "script": SCRIPT,
    }
    page = fill(
        SHARE,
        raw,
        og_type="video.other" if media == "video" else "website",
        title=share.title,
        summary=f"{day} · {duration} · Shared with Snapbak",
        page_url=page_url,
        width=width,
        height=height,
        created_iso=created.strftime(UTC_FORMAT),
        created_text=day,
        duration=duration,
        people=f" · {people}" if people else "",
        home_url=home_url,
        download=download,
    )
    return page


def render_error(state: PageState, home_url: str) -> tuple[str, int]:
    status, message, hint, strokes = ERRORS[state]
    raw = {"glyph": ICON.format(strokes), "refresh": REFRESH if state == "uploading" else ""}
    page = fill(ERROR, raw, message=message, hint=hint, home_url=home_url)
    return page, status


def size_line(bytes: int | None) -> str:
    """A GIF's size as Finder counts it ("4.2 MB"), for the summary line a length would sit in (G7)."""
    if not bytes:
        return "GIF"
    if bytes >= 1e9:
        return f"{bytes / 1e9:.1f} GB"
    megabytes = bytes / 1e6
    return f"{megabytes:.1f} MB" if megabytes < 10 else f"{round(megabytes)} MB"


def attachment(title: str, extension: str = "mp4") -> str:
    """`Content-Disposition` for the download: an ASCII name with nothing a header could break on, and the real
    name in the UTF-8 form (RFC 6266)."""
    name = f"{title}.{extension}"
    plain = "".join(char if char.isascii() and (char.isalnum() or char in " .-") else "_" for char in name)
    said = f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"
    return said


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


async def live_share(db: Session, request: Request, slug: str) -> Share | Response:
    """The share when its page is live, else its error page. A malformed slug is never looked up."""
    share = await db.scalar(select(Share).where(Share.slug == slug)) if is_slug(slug) else None
    state: PageState = "gone" if share is None else page_state(share, datetime.now(UTC))
    if share is not None and state == "live":
        return share
    answer = unavailable(request, state)
    return answer


def unavailable(request: Request, state: PageState, status: int | None = None) -> HTMLResponse:
    """The error page for `state`, with its own status unless `status` is given (a 405 on the share host)."""
    body, own = render_error(state, cloud_deps.cloud(request).config.home_url)
    answer = HTMLResponse(body, status_code=status or own, headers=page_headers(ERROR_POLICY))
    return answer


def signed(request: Request, key: str, disposition: str | None = None) -> str:
    url = cloud_deps.cloud(request).storage.presign_get(key, SIGNED_S, disposition)
    return url


async def media_of(db: Session, share: Share) -> tuple[str | None, Media]:
    """The object the page plays: an audio recording's own completed audio upload (D12, D16), else the share's
    video; None while it isn't there yet."""
    recording = await db.get(Recording, share.recording_id)
    if recording is not None and recording.recording_type == "gif":
        return share.s3_key, "gif"
    if recording is None or recording.recording_type != "audio":
        return share.s3_key, "video"
    key = await db.scalar(
        select(Upload.s3_key)
        .where(Upload.recording_id == share.recording_id, Upload.kind == "audio", Upload.state == "complete")
        .order_by(Upload.completed_at.desc())
        .limit(1)
    )
    return key, "audio"


async def playable(db: Session, request: Request, slug: str) -> tuple[Share, str, Media] | Response:
    """The live share with the key its player streams, or its error page; media not uploaded yet is `uploading`."""
    found = await live_share(db, request, slug)
    if isinstance(found, Response):
        return found
    key, media = await media_of(db, found)
    if key is None:
        return unavailable(request, "uploading")
    return found, key, media


async def sections_of(db: Session, share: Share) -> tuple[str, str, list[float]]:
    """A meeting share's minutes and transcript (D16), and its speakers for the subtitle; nothing for a plain video
    or audio share."""
    if share.type != "meeting":
        return "", "", []
    meeting = await meetings.live_meeting(db, share.recording_id)
    minutes = None if meeting is None else meetings.minutes_of(meeting)
    speakers = await recordings.speakers_of(db, share.recording_id)
    lines = await recordings.live_lines(db, share.recording_id)
    said = [(line.start_ms, speaker_of(speakers.get(line.speaker_id)), line.text) for line in lines]
    bars = speech_bars([(line.start_ms, line.end_ms) for line in lines], share.duration_ms or 0)
    plain = None if minutes is None else people.rendered_doc(minutes, speakers, lines)
    made = meeting_sections(plain, said), speakers_in(said), bars
    return made


@router.api_route("/v/{slug}", methods=READ)
async def page(slug: str, db: Session, request: Request) -> Response:
    found = await playable(db, request, slug)
    if isinstance(found, Response):
        return found
    share, key, media = found
    config = cloud_deps.cloud(request).config
    url = signed(request, key)
    poster = None if share.poster_s3_key is None else signed(request, share.poster_s3_key)
    sections, people, bars = await sections_of(db, share)
    body = render_page(share, config.share_url, url, poster, config.home_url, media, sections, people, bars)
    policy = page_policy(origin(url))
    answer = HTMLResponse(body, headers=page_headers(policy))
    return answer


async def redirect(
    db: Session, request: Request, slug: str, part: Literal["video", "poster", "download"], cache: str
) -> Response:
    """302 to a fresh signed URL: link previews cache the stable route for days, never the signed one."""
    found = await playable(db, request, slug)
    if isinstance(found, Response):
        return found
    share, key, media = found
    if part == "poster":
        if share.poster_s3_key is None:
            return unavailable(request, "gone")
        key = share.poster_s3_key
    disposition = attachment(share.title, EXTENSIONS[media]) if part == "download" else None
    url = signed(request, key, disposition)
    answer = RedirectResponse(url, status_code=302, headers={"Cache-Control": cache})
    return answer


@router.api_route("/v/{slug}/video.mp4", methods=READ)
async def video(slug: str, db: Session, request: Request) -> Response:
    answer = await redirect(db, request, slug, "video", "no-store")
    return answer


@router.api_route("/v/{slug}/animation.gif", methods=READ)
async def animation(slug: str, db: Session, request: Request) -> Response:
    """The GIF itself, for the page's img and the unfurl's og:image (GIFS-AND-REPLAY-60, G7). The share's key
    is the animation, so this is the video route under a GIF name."""
    answer = await redirect(db, request, slug, "video", "no-store")
    return answer


@router.api_route("/v/{slug}/poster.jpg", methods=READ)
async def poster(slug: str, db: Session, request: Request) -> Response:
    answer = await redirect(db, request, slug, "poster", "public, max-age=300")
    return answer


@router.api_route("/v/{slug}/download", methods=READ)
async def download(slug: str, db: Session, request: Request) -> Response:
    answer = await redirect(db, request, slug, "download", "no-store")
    return answer


@router.api_route("/v/{slug}/src", methods=READ)
async def source(slug: str, db: Session, request: Request) -> Response:
    """A fresh signed URL for the player, when the one in the page expired mid-view (`video` names it for either
    player)."""
    found = await playable(db, request, slug)
    if isinstance(found, Response):
        return found
    _, key, _ = found
    when = datetime.now(UTC) + timedelta(seconds=SIGNED_S)
    fresh = {"video": signed(request, key), "expiresAt": when.strftime(UTC_FORMAT)}
    answer = JSONResponse(fresh, headers=NO_STORE)
    return answer


@router.api_route("/og.jpg", methods=READ)
async def card() -> Response:
    answer = Response(OG_CARD, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
    return answer


@router.get("/robots.txt")
async def robots() -> PlainTextResponse:
    return PlainTextResponse(ROBOTS)


@router.get("/")
async def home(request: Request) -> RedirectResponse:
    home_url = cloud_deps.cloud(request).config.home_url
    answer = RedirectResponse(home_url, status_code=302)
    return answer
