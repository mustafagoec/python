import json
import time
from pathlib import Path

import httpx2 as httpx
import pytest
from mcp.client import Client

from social_mcp.config import Settings, TokenStore
from social_mcp.server import create_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        token_file=tmp_path / "tokens.json",
        google_client_id="gid", google_client_secret="gsecret",
        linkedin_api_version="202608", instagram_api_version="v23.0",
    )


def make_server(settings: Settings, handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return create_server(settings, http=http)


def payload(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


async def test_lists_all_tools(settings):
    async with Client(make_server(settings, lambda r: httpx.Response(500))) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"social_status", "linkedin_create_post", "youtube_upload_video", "instagram_publish_image"} <= names


async def test_status_without_tokens(settings):
    async with Client(make_server(settings, lambda r: httpx.Response(500))) as client:
        data = payload(await client.call_tool("social_status", {}))
    assert data["linkedin"] == {"connected": False, "connect_with": "social-mcp auth linkedin"}


async def test_missing_connection_is_reported_as_tool_error(settings):
    async with Client(make_server(settings, lambda r: httpx.Response(500))) as client:
        result = await client.call_tool("youtube_get_channel", {})
    assert result.is_error
    assert "social-mcp auth youtube" in result.content[0].text


async def test_linkedin_post(settings):
    TokenStore(settings.token_file).set("linkedin", {
        "access_token": "li-tok", "expires_at": time.time() + 3600, "person_urn": "urn:li:person:abc",
    })
    seen = {}

    def handler(request: httpx.Request):
        assert request.url.path == "/rest/posts"
        assert request.headers["Authorization"] == "Bearer li-tok"
        assert request.headers["LinkedIn-Version"] == "202608"
        seen.update(json.loads(request.content))
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:42"})

    async with Client(make_server(settings, handler)) as client:
        data = payload(await client.call_tool("linkedin_create_post", {
            "text": "Hallo LinkedIn", "article_url": "https://example.com", "article_title": "Beispiel",
        }))
    assert data["post_urn"] == "urn:li:share:42"
    assert seen["author"] == "urn:li:person:abc"
    assert seen["commentary"] == "Hallo LinkedIn"
    assert seen["content"]["article"] == {"source": "https://example.com", "title": "Beispiel"}


async def test_youtube_refreshes_expired_token(settings):
    store = TokenStore(settings.token_file)
    store.set("youtube", {"access_token": "old", "refresh_token": "r1", "expires_at": time.time() - 10})

    def handler(request: httpx.Request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer new"
        return httpx.Response(200, json={"items": [{
            "id": "UC1", "snippet": {"title": "Mein Kanal"}, "statistics": {"subscriberCount": "10"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}},
        }]})

    async with Client(make_server(settings, handler)) as client:
        data = payload(await client.call_tool("youtube_get_channel", {}))
    assert data["title"] == "Mein Kanal"
    assert store.get("youtube")["access_token"] == "new"
    assert oct(settings.token_file.stat().st_mode & 0o777) == "0o600"


async def test_youtube_api_error_message(settings):
    TokenStore(settings.token_file).set("youtube", {"access_token": "t", "expires_at": time.time() + 3600})

    def handler(request):
        return httpx.Response(403, json={"error": {"message": "quotaExceeded"}})

    async with Client(make_server(settings, handler)) as client:
        result = await client.call_tool("youtube_list_comments", {"video_id": "v1"})
    assert result.is_error
    assert "youtube API 403: quotaExceeded" in result.content[0].text


async def test_instagram_publish_image(settings):
    TokenStore(settings.token_file).set("instagram", {
        "access_token": "ig", "expires_at": time.time() + 50 * 86400, "issued_at": time.time(), "user_id": "17841",
    })
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        assert request.url.params["access_token"] == "ig"
        path = request.url.path
        if path == "/v23.0/17841/media":
            assert request.url.params["image_url"] == "https://example.com/a.jpg"
            return httpx.Response(200, json={"id": "c1"})
        if path == "/v23.0/c1":
            return httpx.Response(200, json={"status_code": "FINISHED", "id": "c1"})
        if path == "/v23.0/17841/media_publish":
            return httpx.Response(200, json={"id": "m1"})
        if path == "/v23.0/m1":
            return httpx.Response(200, json={"id": "m1", "permalink": "https://instagram.com/p/x"})
        return httpx.Response(404)

    async with Client(make_server(settings, handler)) as client:
        data = payload(await client.call_tool("instagram_publish_image", {
            "image_url": "https://example.com/a.jpg", "caption": "Hallo",
        }))
    assert data == {"media_id": "m1", "permalink": "https://instagram.com/p/x"}
    assert ("POST", "/v23.0/17841/media_publish") in calls
