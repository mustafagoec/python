"""Instagram API with Instagram Login (Business and Creator accounts).

Publishing requires media that is reachable under a public HTTPS URL; Instagram fetches it
from there, local files cannot be uploaded directly.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx2 as httpx

from .config import ApiError, Settings, TokenStore, expires_at, raise_for_status

AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
SHORT_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
GRAPH = "https://graph.instagram.com"
SCOPES = [
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_comments",
    "instagram_business_manage_insights",
]
MEDIA_FIELDS = "id,caption,media_type,media_url,permalink,thumbnail_url,timestamp,like_count,comments_count"
# Long-lived tokens last 60 days and can be refreshed once they are at least 24 hours old.
REFRESH_WHEN_LEFT = 10 * 24 * 3600
MIN_TOKEN_AGE = 24 * 3600


class InstagramClient:
    platform = "instagram"

    def __init__(self, settings: Settings, store: TokenStore, http: httpx.AsyncClient):
        self.settings = settings
        self.store = store
        self.http = http

    @property
    def base(self) -> str:
        return f"{GRAPH}/{self.settings.instagram_api_version}"

    async def _token(self) -> dict[str, Any]:
        tok = self.store.get(self.platform)
        if not tok:
            raise ApiError("Instagram ist nicht verbunden. Bitte `social-mcp auth instagram` ausführen.")
        now = time.time()
        exp = tok.get("expires_at")
        if exp and exp < now:
            raise ApiError("Instagram-Token ist abgelaufen. Bitte `social-mcp auth instagram` erneut ausführen.")
        if exp and exp - now < REFRESH_WHEN_LEFT and now - tok.get("issued_at", 0) > MIN_TOKEN_AGE:
            resp = await self.http.get(f"{GRAPH}/refresh_access_token", params={
                "grant_type": "ig_refresh_token", "access_token": tok["access_token"],
            })
            if resp.is_success:
                data = resp.json()
                tok.update(access_token=data["access_token"], expires_at=expires_at(data.get("expires_in")), issued_at=now)
                self.store.set(self.platform, tok)
        return tok

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        tok = await self._token()
        resp = await self.http.request(
            method, f"{self.base}/{path}", params={**(params or {}), "access_token": tok["access_token"]},
        )
        raise_for_status(resp, self.platform)
        return resp.json()

    async def _user_id(self) -> str:
        tok = await self._token()
        if not tok.get("user_id"):
            tok["user_id"] = (await self._request("GET", "me", {"fields": "user_id"}))["user_id"]
            self.store.set(self.platform, tok)
        return tok["user_id"]

    async def get_profile(self) -> dict[str, Any]:
        return await self._request("GET", "me", {
            "fields": "user_id,username,name,account_type,biography,website,profile_picture_url,followers_count,follows_count,media_count",
        })

    async def list_media(self, limit: int = 12) -> list[dict[str, Any]]:
        data = await self._request("GET", "me/media", {"fields": MEDIA_FIELDS, "limit": max(1, min(limit, 100))})
        return data.get("data", [])

    async def list_comments(self, media_id: str, limit: int = 50) -> list[dict[str, Any]]:
        data = await self._request("GET", f"{media_id}/comments", {
            "fields": "id,text,username,timestamp,like_count,replies{id,text,username,timestamp}",
            "limit": max(1, min(limit, 100)),
        })
        return data.get("data", [])

    async def reply_to_comment(self, comment_id: str, text: str) -> dict[str, Any]:
        data = await self._request("POST", f"{comment_id}/replies", {"message": text})
        return {"comment_id": data["id"], "parent_id": comment_id}

    async def get_media_insights(self, media_id: str, metrics: list[str] | None = None) -> dict[str, Any]:
        metrics = metrics or ["reach", "likes", "comments", "saved", "shares", "total_interactions", "views"]
        data = await self._request("GET", f"{media_id}/insights", {"metric": ",".join(metrics)})
        return {m["name"]: m["values"][0]["value"] if m.get("values") else m.get("total_value", {}).get("value") for m in data.get("data", [])}

    async def _publish(self, container_params: dict[str, Any], timeout_s: float = 300) -> dict[str, Any]:
        user_id = await self._user_id()
        container = await self._request("POST", f"{user_id}/media", container_params)
        creation_id = container["id"]
        deadline = time.monotonic() + timeout_s
        while True:
            status = await self._request("GET", creation_id, {"fields": "status_code,status"})
            code = status.get("status_code")
            if code in (None, "FINISHED"):
                break
            if code in ("ERROR", "EXPIRED"):
                raise ApiError(f"Instagram konnte die Medien nicht verarbeiten: {status.get('status', code)}")
            if time.monotonic() > deadline:
                raise ApiError(f"Instagram-Verarbeitung dauert zu lange (Container {creation_id}, Status {code}).")
            await asyncio.sleep(3)
        published = await self._request("POST", f"{user_id}/media_publish", {"creation_id": creation_id})
        media = await self._request("GET", published["id"], {"fields": "id,permalink"})
        return {"media_id": media["id"], "permalink": media.get("permalink")}

    async def publish_image(self, image_url: str, caption: str = "", alt_text: str | None = None) -> dict[str, Any]:
        params = {"image_url": image_url, "caption": caption}
        if alt_text:
            params["alt_text"] = alt_text
        return await self._publish(params)

    async def publish_reel(self, video_url: str, caption: str = "", share_to_feed: bool = True) -> dict[str, Any]:
        return await self._publish({
            "media_type": "REELS", "video_url": video_url, "caption": caption,
            "share_to_feed": "true" if share_to_feed else "false",
        }, timeout_s=600)

    async def publish_carousel(self, image_urls: list[str], caption: str = "") -> dict[str, Any]:
        if not 2 <= len(image_urls) <= 10:
            raise ApiError("Ein Karussell braucht 2 bis 10 Bilder.")
        user_id = await self._user_id()
        children = []
        for url in image_urls:
            child = await self._request("POST", f"{user_id}/media", {"image_url": url, "is_carousel_item": "true"})
            children.append(child["id"])
        return await self._publish({"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption})
