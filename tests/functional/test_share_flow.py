"""A share from the app's side to the viewer's: the routes in-process on the compose stack, then the page, its
helper routes and each state it can be in (SHR-01, CLD-04; 12 LLD, Public page routes)."""

import statistics
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import (
    RECORDING,
    S3_HTTP,
    VIDEO_SIZE,
    Sql,
    create_share,
    put_recording,
    register,
    share_live,
    upload_share,
)
from sqlalchemy import event

from snapbak_backend.api import share_page

PAGE_HEADERS = {
    "content-type": "text/html; charset=utf-8",
    "cache-control": "private, no-store",
    "x-robots-tag": "noindex, nofollow",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "strict-transport-security": "max-age=31536000",
}
ERROR_POLICY = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
VIDEO = b"\x00\x00\x00\x20ftypisom fast-start mp4"


def headers_of(response: Any) -> dict[str, str]:
    return {name: response.headers[name] for name in PAGE_HEADERS}


def get(api: Any, path: str) -> Any:
    return api.get(path, follow_redirects=False)


def test_shr01_the_live_page_plays_the_signed_video_with_its_headers(api: Any) -> None:
    auth = register(api)
    made = share_live(api, auth, VIDEO)
    page = get(api, f"/v/{made['slug']}")
    assert page.status_code == 200
    assert headers_of(page) == PAGE_HEADERS
    config = api.app.state.cloud.config
    bucket = f"https://{config.bucket}.s3.{config.s3_region}.amazonaws.com"
    assert page.headers["content-security-policy"] == (
        f"default-src 'none'; img-src data: {bucket}; media-src {bucket}; "
        f"style-src 'unsafe-inline'; script-src '{share_page.SCRIPT_HASH}'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    )
    assert '<h1 title="Recording 2026-10-17 at 10.02.11">Recording 2026-10-17 at 10.02.11</h1>' in page.text
    assert f'<meta property="og:video" content="http://testserver/v/{made["slug"]}/video.mp4">' in page.text
    src = page.text.split(' src="')[1].split('"')[0].replace("&amp;", "&")
    assert S3_HTTP.get(src).content == VIDEO
    poster = page.text.split(' poster="')[1].split('"')[0].replace("&amp;", "&")
    assert f"/shares/{made['shareId']}/v1/poster.jpg?" in poster
    assert "X-Amz-Signature=" in poster
    assert S3_HTTP.get(poster).content == b"jpeg"


def test_shr01_the_helper_routes_redirect_to_fresh_signed_urls(api: Any) -> None:
    auth = register(api)
    slug = share_live(api, auth, VIDEO, poster=b"\xff\xd8 jpeg")["slug"]
    video = get(api, f"/v/{slug}/video.mp4")
    poster = get(api, f"/v/{slug}/poster.jpg")
    download = get(api, f"/v/{slug}/download")
    source = get(api, f"/v/{slug}/src")
    assert [(r.status_code, r.headers["cache-control"]) for r in (video, poster, download, source)] == [
        (302, "no-store"),
        (302, "public, max-age=300"),
        (302, "no-store"),
        (200, "no-store"),
    ]
    assert S3_HTTP.get(video.headers["location"]).content == VIDEO
    assert S3_HTTP.get(poster.headers["location"]).content == b"\xff\xd8 jpeg"
    assert "response-content-disposition" not in poster.headers["location"]
    assert "response-content-disposition" not in video.headers["location"]
    assert "response-content-disposition=attachment" in download.headers["location"]
    saved = S3_HTTP.get(download.headers["location"])
    assert (saved.content, saved.headers["content-disposition"]) == (
        VIDEO,
        'attachment; filename="Recording 2026-10-17 at 10.02.11.mp4"; '
        "filename*=UTF-8''Recording%202026-10-17%20at%2010.02.11.mp4",
    )
    fresh = source.json()
    assert fresh["expiresAt"].endswith("Z")
    assert S3_HTTP.get(fresh["video"]).content == VIDEO
    for signed in (fresh["video"], video.headers["location"], download.headers["location"]):
        assert "X-Amz-Expires=3600" in signed, signed
    expires = datetime.fromisoformat(fresh["expiresAt"])
    assert abs(expires - (datetime.now(UTC) + timedelta(hours=1))) < timedelta(seconds=5)


def assert_state(api: Any, slug: str, status: int, message: str) -> None:
    """The page and every helper route answer `status`, the page with `message`."""
    page = get(api, f"/v/{slug}")
    assert (page.status_code, headers_of(page)) == (status, PAGE_HEADERS)
    assert page.headers["content-security-policy"] == ERROR_POLICY
    assert f"<h1>{message}</h1>" in page.text
    assert page.text.count('href="https://snapbak.app?ref=share"') == 2
    assert "<video" not in page.text
    for helper in ("video.mp4", "poster.jpg", "download", "src"):
        assert get(api, f"/v/{slug}/{helper}").status_code == status, helper


def test_shr01_a_share_still_uploading_its_first_video_is_not_ready(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    slug = create_share(api, auth).json()["slug"]
    assert_state(api, slug, 404, "This video isn&#x27;t ready yet")


def test_shr01_turned_off_is_410_and_turned_on_again_plays(api: Any) -> None:
    auth = register(api)
    slug = share_live(api, auth)["slug"]
    api.patch(f"/v1/shares/{slug}", json={"state": "disabled"}, headers=auth)
    assert_state(api, slug, 410, "This link was turned off")
    api.patch(f"/v1/shares/{slug}", json={"state": "live"}, headers=auth)
    assert get(api, f"/v/{slug}").status_code == 200


def test_shr01_expired_is_410_and_a_new_expiry_revives_it(api: Any, sql: Sql) -> None:
    auth = register(api)
    slug = share_live(api, auth)["slug"]
    sql("UPDATE shares SET expires_at = now() - interval '1 second'")
    assert_state(api, slug, 410, "This link has expired")
    api.patch(f"/v1/shares/{slug}", json={"expiresIn": "7d"}, headers=auth)
    assert get(api, f"/v/{slug}").status_code == 200


def test_cld04_a_deleted_share_is_404(api: Any) -> None:
    auth = register(api)
    slug = share_live(api, auth)["slug"]
    api.delete(f"/v1/shares/{slug}", headers=auth)
    assert_state(api, slug, 404, "This video isn&#x27;t available")


def test_cld04_a_trashed_recording_takes_its_page_down(api: Any) -> None:
    auth = register(api)
    slug = share_live(api, auth)["slug"]
    api.delete(f"/v1/recordings/{RECORDING}", headers=auth)
    assert_state(api, slug, 404, "This video isn&#x27;t available")


def test_shr01_an_update_in_flight_keeps_playing_the_previous_version(api: Any) -> None:
    auth = register(api)
    made = share_live(api, auth, b"version one")
    create_share(api, auth)
    body = {"kind": "share", "bytes": 11, "contentType": "video/mp4", "shareId": made["shareId"], **VIDEO_SIZE}
    api.post(f"/v1/recordings/{RECORDING}/uploads", json=body, headers=auth)
    video = get(api, f"/v/{made['slug']}/video.mp4")
    assert S3_HTTP.get(video.headers["location"]).content == b"version one"
    upload_share(api, auth, made["shareId"], b"version two")
    video = get(api, f"/v/{made['slug']}/video.mp4")
    assert S3_HTTP.get(video.headers["location"]).content == b"version two"


def test_shr01_without_a_poster_the_page_still_plays_and_the_poster_route_is_404(api: Any) -> None:
    auth = register(api)
    slug = share_live(api, auth, poster=None)["slug"]
    page = get(api, f"/v/{slug}")
    assert page.status_code == 200
    assert '<meta property="og:image" content="http://testserver/og.jpg">' in page.text
    missing = get(api, f"/v/{slug}/poster.jpg")
    assert missing.status_code == 404
    assert (get(api, f"/v/{slug}/video.mp4").status_code, get(api, f"/v/{slug}/download").status_code) == (302, 302)
    assert "<h1>This video isn&#x27;t available</h1>" in missing.text


@pytest.mark.parametrize("slug", ["short", "Vb3kQx9_mT2LpZaR.", "x" * 65, "..%5C..%5Cetc%5Cpasswd-padding"])
def test_shr01_a_malformed_slug_is_404_without_a_database_read(api: Any, slug: str) -> None:
    statements: list[str] = []

    def seen(*args: Any) -> None:
        statements.append(args[2])

    engine = api.app.state.cloud.engine.sync_engine
    event.listen(engine, "before_cursor_execute", seen)
    try:
        page = get(api, f"/v/{slug}")
    finally:
        event.remove(engine, "before_cursor_execute", seen)
    assert (page.status_code, statements) == (404, [])
    assert "This video isn&#x27;t available" in page.text


def test_shr01_an_unknown_slug_is_404(api: Any) -> None:
    assert get(api, f"/v/{'A' * 16}").status_code == 404


def test_shr01_robots_keep_crawlers_off_the_pages_and_the_root_goes_home(api: Any) -> None:
    robots = get(api, "/robots.txt")
    root = get(api, "/")
    assert (robots.status_code, robots.text) == (200, "User-agent: *\nDisallow: /v/\n")
    assert (root.status_code, root.headers["location"]) == (302, "https://snapbak.app")


def test_shr01_the_page_answers_well_inside_its_200_ms_cap(api: Any) -> None:
    auth = register(api)
    slug = share_live(api, auth)["slug"]
    get(api, f"/v/{slug}")
    times = []
    for _ in range(30):
        started = time.perf_counter()
        assert get(api, f"/v/{slug}").status_code == 200
        times.append((time.perf_counter() - started) * 1000)
    median = statistics.median(times)
    print(f"share page in-process: median {median:.1f} ms, max {max(times):.1f} ms")
    assert median < 200, times


def test_shr01_another_devices_cannot_see_a_share_through_the_api_but_anyone_can_watch(api: Any) -> None:
    auth = register(api)
    slug = share_live(api, auth)["slug"]
    stranger = register(api, str(uuid.uuid4()))
    assert api.get(f"/v1/recordings/{RECORDING}/shares", headers=stranger).status_code == 404
    assert get(api, f"/v/{slug}").status_code == 200


@pytest.mark.parametrize("path", ["", "/video.mp4", "/poster.jpg", "/download", "/src"])
def test_shr01_head_answers_like_get_without_a_body_for_link_preview_bots(api: Any, path: str) -> None:
    auth = register(api)
    slug = share_live(api, auth)["slug"]
    got = get(api, f"/v/{slug}{path}")
    head = api.head(f"/v/{slug}{path}", follow_redirects=False)
    assert (head.status_code, head.content) == (got.status_code, b"")
    assert head.headers["cache-control"] == got.headers["cache-control"]


def test_shr01_head_on_the_branded_card_answers_like_get(api: Any) -> None:
    head = api.head("/og.jpg")
    assert (head.status_code, head.headers["content-type"], head.content) == (200, "image/jpeg", b"")


def test_shr01_an_unknown_path_on_the_share_host_gets_the_branded_404(api: Any) -> None:
    page = get(api, "/watch/this")
    assert (page.status_code, page.headers["content-type"]) == (404, "text/html; charset=utf-8")
    assert "This video isn&#x27;t available" in page.text


def test_shr01_the_branded_card_is_served_for_unfurls_without_a_poster(api: Any) -> None:
    card = get(api, "/og.jpg")
    assert (card.status_code, card.headers["content-type"], card.headers["cache-control"]) == (
        200,
        "image/jpeg",
        "public, max-age=86400",
    )
    assert card.content == share_page.OG_CARD


def test_shr01_a_wrong_method_on_the_share_host_gets_the_branded_page_with_405(api: Any) -> None:
    page = api.post("/v/AAAAAAAAAAAAAAAA")
    assert (page.status_code, page.headers["content-type"]) == (405, "text/html; charset=utf-8")
    assert sorted(page.headers["allow"].split(", ")) == ["GET", "HEAD"]
    assert "This video isn&#x27;t available" in page.text


@pytest.mark.parametrize(
    ("method", "path", "status"), [("GET", "/watch/this", 404), ("POST", "/v/AAAAAAAAAAAAAAAA", 405)]
)
def test_shr01_on_any_other_host_unknown_paths_keep_fastapis_json(
    api: Any, method: str, path: str, status: int
) -> None:
    answer = api.request(method, path, headers={"Host": "api.example"})
    assert (answer.status_code, answer.headers["content-type"]) == (status, "application/json")
