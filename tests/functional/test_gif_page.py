"""A GIF share's page and helper routes (GIFS-AND-REPLAY-60, G7): the img player, the animated unfurl, the
download as the .gif itself, and each state the link can be in."""

from typing import Any

from conftest import S3_HTTP, gif_live, register


def get(api: Any, path: str) -> Any:
    return api.get(path, follow_redirects=False)


def test_g7_the_live_page_plays_the_gif_and_unfurls_it_animated(api: Any) -> None:
    auth = register(api)
    made = gif_live(api, auth)
    page = get(api, f"/v/{made['slug']}")
    assert page.status_code == 200
    assert '<div class="player plate"><img src=' in page.text
    assert f'<meta property="og:image" content="http://testserver/v/{made["slug"]}/animation.gif">' in page.text
    assert '<meta property="og:image:type" content="image/gif">' in page.text
    assert "og:video" not in page.text
    assert page.text.count("og.jpg") == 0, "the GIF animates the unfurl: no branded card"
    assert ">Download GIF<" in page.text
    assert 'width="600" height="386"' in page.text
    assert 'alt="Recording 2026-10-17 at 10.02.11"' in page.text


def test_g7_the_animation_route_signs_the_gif_and_the_download_serves_the_file(api: Any) -> None:
    auth = register(api)
    made = gif_live(api, auth)
    animation = get(api, f"/v/{made['slug']}/animation.gif")
    assert animation.status_code == 302
    assert animation.headers["cache-control"] == "no-store"
    assert "X-Amz-Signature=" in animation.headers["location"]
    download = get(api, f"/v/{made['slug']}/download")
    assert download.status_code == 302
    assert "response-content-disposition=attachment" in download.headers["location"]
    saved = S3_HTTP.get(download.headers["location"])
    assert (saved.content, saved.headers["content-disposition"]) == (
        b"GIF89a animation",
        'attachment; filename="Recording 2026-10-17 at 10.02.11.gif"; '
        "filename*=UTF-8''Recording%202026-10-17%20at%2010.02.11.gif",
    )


def test_g7_a_disabled_gif_shows_the_turned_off_page(api: Any) -> None:
    auth = register(api)
    made = gif_live(api, auth)
    assert api.patch(f"/v1/shares/{made['slug']}", json={"state": "disabled"}, headers=auth).status_code == 200
    page = get(api, f"/v/{made['slug']}")
    assert page.status_code == 410
    assert "This link was turned off" in page.text
    assert '<meta http-equiv="refresh"' not in page.text
