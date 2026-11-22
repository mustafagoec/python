"""YouTube Data API v3 for the signed-in channel."""

from __future__ import annotations

import mimetypes
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import httpx2 as httpx

from .config import ApiError, Settings, TokenStore, expires_at, raise_for_status

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.upload",
]


def _video_summary(item: dict[str, Any]) -> dict[str, Any]:
    snippet = item.get("snippet", {})
    return {
        "id": item["id"] if isinstance(item["id"], str) else item["id"].get("videoId"),
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "published_at": snippet.get("publishedAt"),
        "tags": snippet.get("tags", []),
        "privacy_status": item.get("status", {}).get("privacyStatus"),
        "statistics": item.get("statistics", {}),
        "duration": item.get("contentDetails", {}).get("duration"),
    }


def _comment_summary(thread: dict[str, Any]) -> dict[str, Any]:
    top = thread["snippet"]["topLevelComment"]
    s = top["snippet"]
    return {
        "comment_id": top["id"],
        "author": s.get("authorDisplayName"),
        "text": s.get("textOriginal") or s.get("textDisplay"),
        "like_count": s.get("likeCount"),
        "published_at": s.get("publishedAt"),
        "reply_count": thread["snippet"].get("totalReplyCount", 0),
        "replies": [
            {"comment_id": r["id"], "author": r["snippet"].get("authorDisplayName"), "text": r["snippet"].get("textOriginal")}
            for r in thread.get("replies", {}).get("comments", [])
        ],
    }


class YouTubeClient:
    platform = "youtube"

    def __init__(self, settings: Settings, store: TokenStore, http: httpx.AsyncClient):
        self.settings = settings
        self.store = store
        self.http = http

    async def _access_token(self) -> str:
        tok = self.store.get(self.platform)
        if not tok:
            raise ApiError("YouTube ist nicht verbunden. Bitte `social-mcp auth youtube` ausführen.")
        if tok.get("expires_at") and tok["expires_at"] < time.time() + 60:
            if not tok.get("refresh_token"):
                raise ApiError("YouTube-Token ist abgelaufen. Bitte `social-mcp auth youtube` erneut ausführen.")
            resp = await self.http.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
            })
            raise_for_status(resp, self.platform)
            data = resp.json()
            tok.update(access_token=data["access_token"], expires_at=expires_at(data.get("expires_in")))
            self.store.set(self.platform, tok)
        return tok["access_token"]

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self._access_token()}"}
        resp = await self.http.request(method, f"{API}/{path}", headers=headers, **kwargs)
        raise_for_status(resp, self.platform)
        return resp.json() if resp.content else {}

    async def get_channel(self) -> dict[str, Any]:
        data = await self._request("GET", "channels", params={"part": "snippet,statistics,contentDetails", "mine": "true"})
        if not data.get("items"):
            raise ApiError("Zu diesem Google-Konto gibt es keinen YouTube-Kanal.")
        ch = data["items"][0]
        return {
            "id": ch["id"],
            "title": ch["snippet"].get("title"),
            "description": ch["snippet"].get("description"),
            "custom_url": ch["snippet"].get("customUrl"),
            "statistics": ch.get("statistics", {}),
            "uploads_playlist_id": ch["contentDetails"]["relatedPlaylists"]["uploads"],
        }

    async def list_my_videos(self, max_results: int = 10) -> list[dict[str, Any]]:
        uploads = (await self.get_channel())["uploads_playlist_id"]
        items = await self._request("GET", "playlistItems", params={
            "part": "contentDetails", "playlistId": uploads, "maxResults": max(1, min(max_results, 50)),
        })
        ids = [i["contentDetails"]["videoId"] for i in items.get("items", [])]
        return await self.get_videos(ids) if ids else []

    async def get_videos(self, video_ids: list[str]) -> list[dict[str, Any]]:
        data = await self._request("GET", "videos", params={
            "part": "snippet,statistics,status,contentDetails", "id": ",".join(video_ids[:50]),
        })
        return [_video_summary(v) for v in data.get("items", [])]

    async def list_comments(self, video_id: str, max_results: int = 20, order: Literal["time", "relevance"] = "time") -> list[dict[str, Any]]:
        data = await self._request("GET", "commentThreads", params={
            "part": "snippet,replies", "videoId": video_id, "maxResults": max(1, min(max_results, 100)),
            "order": order, "textFormat": "plainText",
        })
        return [_comment_summary(t) for t in data.get("items", [])]

    async def post_comment(self, video_id: str, text: str) -> dict[str, Any]:
        data = await self._request("POST", "commentThreads", params={"part": "snippet"}, json={
            "snippet": {"videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": text}}},
        })
        return {"comment_id": data["snippet"]["topLevelComment"]["id"], "video_id": video_id}

    async def reply_to_comment(self, parent_comment_id: str, text: str) -> dict[str, Any]:
        data = await self._request("POST", "comments", params={"part": "snippet"}, json={
            "snippet": {"parentId": parent_comment_id, "textOriginal": text},
        })
        return {"comment_id": data["id"], "parent_id": parent_comment_id}

    async def update_video(
        self,
        video_id: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        privacy_status: Literal["public", "unlisted", "private"] | None = None,
    ) -> dict[str, Any]:
        current = await self._request("GET", "videos", params={"part": "snippet,status", "id": video_id})
        if not current.get("items"):
            raise ApiError(f"Video {video_id} nicht gefunden.")
        video = current["items"][0]
        snippet = {k: video["snippet"][k] for k in ("title", "description", "tags", "categoryId", "defaultLanguage") if k in video["snippet"]}
        if title is not None:
            snippet["title"] = title
        if description is not None:
            snippet["description"] = description
        if tags is not None:
            snippet["tags"] = tags
        body: dict[str, Any] = {"id": video_id, "snippet": snippet}
        parts = ["snippet"]
        if privacy_status is not None:
            body["status"] = {**video["status"], "privacyStatus": privacy_status}
            parts.append("status")
        data = await self._request("PUT", "videos", params={"part": ",".join(parts)}, json=body)
        return _video_summary(data)

    async def upload_video(
        self,
        file_path: str,
        title: str,
        description: str = "",
        tags: list[str] | None = None,
        privacy_status: Literal["public", "unlisted", "private"] = "private",
        category_id: str = "22",
        made_for_kids: bool = False,
    ) -> dict[str, Any]:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise ApiError(f"Videodatei nicht gefunden: {path}")
        size = path.stat().st_size
        mime = mimetypes.guess_type(path.name)[0] or "video/*"
        token = await self._access_token()

        start = await self.http.post(
            UPLOAD_API,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {token}",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": mime,
            },
            json={
                "snippet": {"title": title, "description": description, "tags": tags or [], "categoryId": category_id},
                "status": {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": made_for_kids},
            },
        )
        raise_for_status(start, self.platform)
        upload_url = start.headers["location"]

        async def chunks() -> AsyncIterator[bytes]:
            with path.open("rb") as fh:
                while chunk := fh.read(8 * 1024 * 1024):
                    yield chunk

        resp = await self.http.put(
            upload_url,
            content=chunks(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": mime, "Content-Length": str(size)},
            timeout=None,
        )
        raise_for_status(resp, self.platform)
        video = resp.json()
        return {"video_id": video["id"], "url": f"https://youtu.be/{video['id']}", "privacy_status": privacy_status}
