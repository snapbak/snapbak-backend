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
from snapbak_backend.db.models import PageState, Share, page_state
from snapbak_backend.schemas.cloud import UTC_FORMAT
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


def clock(ms: int) -> str:
    """`1:02`, or `1:02:03` past an hour."""
    minutes, seconds = divmod(ms // 1000, 60)
    hours, minutes = divmod(minutes, 60)
    shown = f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"
    return shown


def render_page(share: Share, base: str, video_url: str, poster_url: str | None, home_url: str) -> str:
    """The page for a live share. The date is the UTC day the share was made, the same text in the unfurl and on
    the page."""
    page_url = link(base, share.slug)
    created = share.created_at.astimezone(UTC)
    day = f"{created:%b} {created.day}, {created.year}"
    duration = clock(share.duration_ms or 0)
    if poster_url is None:
        image_tags = fill(OG_CARD_TAGS, {}, base=base)
        poster_attr = ""
    else:
        image_tags = fill(POSTER_TAGS, {}, page_url=page_url, title=share.title)
        poster_attr = f' poster="{html.escape(poster_url)}"'
    page = fill(
        SHARE,
        {"image_tags": image_tags, "poster_attr": poster_attr, "script": SCRIPT},
        title=share.title,
        summary=f"{day} · {duration} · Shared with Snapbak",
        page_url=page_url,
        video_url=video_url,
        width=str(share.width),
        height=str(share.height),
        created_iso=created.strftime(UTC_FORMAT),
        created_text=day,
        duration=duration,
        home_url=home_url,
    )
    return page


def render_error(state: PageState, home_url: str) -> tuple[str, int]:
    status, message, hint, strokes = ERRORS[state]
    raw = {"glyph": ICON.format(strokes), "refresh": REFRESH if state == "uploading" else ""}
    page = fill(ERROR, raw, message=message, hint=hint, home_url=home_url)
    return page, status


def attachment(title: str) -> str:
    """`Content-Disposition` for the download: an ASCII name with nothing a header could break on, and the real
    name in the UTF-8 form (RFC 6266)."""
    name = f"{title}.mp4"
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


def signed(request: Request, key: str | None, disposition: str | None = None) -> str:
    """A GET of a live share's object; a live share always has its video."""
    if key is None:
        raise RuntimeError("a live share without its video")
    url = cloud_deps.cloud(request).storage.presign_get(key, SIGNED_S, disposition)
    return url


@router.api_route("/v/{slug}", methods=READ)
async def page(slug: str, db: Session, request: Request) -> Response:
    found = await live_share(db, request, slug)
    if isinstance(found, Response):
        return found
    config = cloud_deps.cloud(request).config
    video = signed(request, found.s3_key)
    poster = None if found.poster_s3_key is None else signed(request, found.poster_s3_key)
    body = render_page(found, config.share_url, video, poster, config.home_url)
    policy = page_policy(origin(video))
    answer = HTMLResponse(body, headers=page_headers(policy))
    return answer


async def redirect(
    db: Session, request: Request, slug: str, part: Literal["video", "poster", "download"], cache: str
) -> Response:
    """302 to a fresh signed URL: link previews cache the stable route for days, never the signed one."""
    found = await live_share(db, request, slug)
    if isinstance(found, Response):
        return found
    if part == "poster" and found.poster_s3_key is None:
        missing = unavailable(request, "gone")
        return missing
    key = found.poster_s3_key if part == "poster" else found.s3_key
    url = signed(request, key, attachment(found.title) if part == "download" else None)
    answer = RedirectResponse(url, status_code=302, headers={"Cache-Control": cache})
    return answer


@router.api_route("/v/{slug}/video.mp4", methods=READ)
async def video(slug: str, db: Session, request: Request) -> Response:
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
    """A fresh signed URL for the player, when the one in the page expired mid-view."""
    found = await live_share(db, request, slug)
    if isinstance(found, Response):
        return found
    when = datetime.now(UTC) + timedelta(seconds=SIGNED_S)
    fresh = {"video": signed(request, found.s3_key), "expiresAt": when.strftime(UTC_FORMAT)}
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
