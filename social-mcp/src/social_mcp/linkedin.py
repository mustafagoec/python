"""LinkedIn: profile and posting on behalf of the signed-in member.

Requires a LinkedIn app with the products "Sign In with LinkedIn using OpenID Connect"
and "Share on LinkedIn" (scopes: openid profile email w_member_social).
"""

from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx2 as httpx

from .config import ApiError, Settings, TokenStore, expires_at, raise_for_status

AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
API = "https://api.linkedin.com"
SCOPES = ["openid", "profile", "email", "w_member_social"]


class LinkedInClient:
    platform = "linkedin"

    def __init__(self, settings: Settings, store: TokenStore, http: httpx.AsyncClient):
        self.settings = settings
        self.store = store
        self.http = http

    async def _token(self) -> dict[str, Any]:
        tok = self.store.get(self.platform)
        if not tok:
            raise ApiError("LinkedIn ist nicht verbunden. Bitte `social-mcp auth linkedin` ausführen.")
        if tok.get("expires_at") and tok["expires_at"] < time.time() + 60:
            if not tok.get("refresh_token"):
                raise ApiError("LinkedIn-Token ist abgelaufen. Bitte `social-mcp auth linkedin` erneut ausführen.")
            resp = await self.http.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
                "client_id": self.settings.linkedin_client_id,
                "client_secret": self.settings.linkedin_client_secret,
            })
            raise_for_status(resp, self.platform)
            data = resp.json()
            tok.update(access_token=data["access_token"], expires_at=expires_at(data.get("expires_in")))
            if data.get("refresh_token"):
                tok["refresh_token"] = data["refresh_token"]
            self.store.set(self.platform, tok)
        return tok

    async def _headers(self) -> dict[str, str]:
        tok = await self._token()
        return {
            "Authorization": f"Bearer {tok['access_token']}",
            "LinkedIn-Version": self.settings.linkedin_api_version,
            "X-Restli-Protocol-Version": "2.0.0",
        }

    async def get_profile(self) -> dict[str, Any]:
        tok = await self._token()
        resp = await self.http.get(f"{API}/v2/userinfo", headers={"Authorization": f"Bearer {tok['access_token']}"})
        raise_for_status(resp, self.platform)
        info = resp.json()
        info["person_urn"] = f"urn:li:person:{info['sub']}"
        if tok.get("person_urn") != info["person_urn"]:
            tok["person_urn"] = info["person_urn"]
            self.store.set(self.platform, tok)
        return info

    async def _author(self) -> str:
        tok = await self._token()
        return tok.get("person_urn") or (await self.get_profile())["person_urn"]

    async def _upload_image(self, author: str, image_path: str) -> str:
        path = Path(image_path).expanduser()
        if not path.is_file():
            raise ApiError(f"Bilddatei nicht gefunden: {path}")
        resp = await self.http.post(
            f"{API}/rest/images",
            params={"action": "initializeUpload"},
            headers=await self._headers(),
            json={"initializeUploadRequest": {"owner": author}},
        )
        raise_for_status(resp, self.platform)
        value = resp.json()["value"]
        tok = await self._token()
        put = await self.http.put(
            value["uploadUrl"],
            content=path.read_bytes(),
            headers={
                "Authorization": f"Bearer {tok['access_token']}",
                "Content-Type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            },
            timeout=300,
        )
        raise_for_status(put, self.platform)
        return value["image"]

    async def create_post(
        self,
        text: str,
        visibility: Literal["PUBLIC", "CONNECTIONS"] = "PUBLIC",
        article_url: str | None = None,
        article_title: str | None = None,
        article_description: str | None = None,
        image_path: str | None = None,
        image_alt_text: str | None = None,
    ) -> dict[str, Any]:
        if article_url and image_path:
            raise ApiError("Ein LinkedIn-Post kann entweder einen Link oder ein Bild enthalten, nicht beides.")
        author = await self._author()
        body: dict[str, Any] = {
            "author": author,
            "commentary": text,
            "visibility": visibility,
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if article_url:
            article = {"source": article_url, "title": article_title or article_url}
            if article_description:
                article["description"] = article_description
            body["content"] = {"article": article}
        elif image_path:
            media = {"id": await self._upload_image(author, image_path)}
            if image_alt_text:
                media["altText"] = image_alt_text
            body["content"] = {"media": media}

        resp = await self.http.post(f"{API}/rest/posts", headers=await self._headers(), json=body)
        raise_for_status(resp, self.platform)
        urn = resp.headers.get("x-restli-id", "")
        return {"post_urn": urn, "url": f"https://www.linkedin.com/feed/update/{urn}/" if urn else None}

    async def delete_post(self, post_urn: str) -> dict[str, Any]:
        headers = await self._headers()
        headers["X-RestLi-Method"] = "DELETE"
        resp = await self.http.delete(f"{API}/rest/posts/{quote(post_urn, safe='')}", headers=headers)
        raise_for_status(resp, self.platform)
        return {"deleted": post_urn}
