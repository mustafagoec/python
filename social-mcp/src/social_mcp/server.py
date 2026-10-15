"""MCP server exposing LinkedIn, YouTube and Instagram tools."""

from __future__ import annotations

import functools
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, ParamSpec, TypeVar

import httpx2 as httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import Settings, TokenStore
from .instagram import InstagramClient
from .linkedin import LinkedInClient
from .youtube import YouTubeClient

P = ParamSpec("P")
R = TypeVar("R")

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

INSTRUCTIONS = """\
Verbindet Claude mit den LinkedIn-, YouTube- und Instagram-Konten des Nutzers.
Rufe zuerst `social_status` auf, um zu sehen, welche Konten verbunden sind.
Alles, was veröffentlicht, kommentiert oder löscht, ist öffentlich sichtbar:
zeige dem Nutzer den finalen Text und hole seine Bestätigung ein, bevor du solche Tools aufrufst.
"""


def _network_errors(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except httpx.HTTPError as exc:
            raise ToolError(f"Netzwerkfehler: {exc}") from exc

    return wrapper


def create_server(settings: Settings | None = None, http: httpx.AsyncClient | None = None) -> MCPServer:
    settings = settings or Settings()
    store = TokenStore(settings.token_file)
    http = http or httpx.AsyncClient(timeout=60, follow_redirects=True)
    li = LinkedInClient(settings, store, http)
    yt = YouTubeClient(settings, store, http)
    ig = InstagramClient(settings, store, http)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await http.aclose()

    mcp = MCPServer("social-media", instructions=INSTRUCTIONS, lifespan=lifespan)

    def tool(annotations: ToolAnnotations) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        def register(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
            mcp.tool(annotations=annotations)(_network_errors(fn))
            return fn

        return register

    # --- General -----------------------------------------------------------------

    @tool(READ)
    async def social_status() -> dict[str, Any]:
        """Zeigt, welche Social-Media-Konten verbunden sind und wann die Tokens ablaufen."""
        result: dict[str, Any] = {}
        for platform in ("linkedin", "youtube", "instagram"):
            tok = store.get(platform)
            if not tok:
                result[platform] = {"connected": False, "connect_with": f"social-mcp auth {platform}"}
                continue
            exp = tok.get("expires_at")
            result[platform] = {
                "connected": True,
                "account": tok.get("account"),
                "expires_in_days": round((exp - time.time()) / 86400, 1) if exp else None,
                "auto_refresh": bool(tok.get("refresh_token")) or platform == "instagram",
            }
        return result

    # --- LinkedIn ----------------------------------------------------------------

    @tool(READ)
    async def linkedin_get_profile() -> dict[str, Any]:
        """Profil des verbundenen LinkedIn-Kontos (Name, E-Mail, Person-URN)."""
        return await li.get_profile()

    @tool(WRITE)
    async def linkedin_create_post(
        text: Annotated[str, Field(description="Text des Posts (max. 3000 Zeichen). Hashtags einfach als #tag schreiben.", max_length=3000)],
        visibility: Annotated[Literal["PUBLIC", "CONNECTIONS"], Field(description="PUBLIC = alle, CONNECTIONS = nur Kontakte")] = "PUBLIC",
        article_url: Annotated[str | None, Field(description="Optional: Link, der als Artikel-Vorschau angehängt wird")] = None,
        article_title: Annotated[str | None, Field(description="Titel der Link-Vorschau")] = None,
        article_description: Annotated[str | None, Field(description="Beschreibung der Link-Vorschau")] = None,
        image_path: Annotated[str | None, Field(description="Optional: lokaler Pfad zu einem Bild (JPG/PNG/GIF)")] = None,
        image_alt_text: Annotated[str | None, Field(description="Alternativtext für das Bild")] = None,
    ) -> dict[str, Any]:
        """Veröffentlicht einen Post im LinkedIn-Feed des Nutzers - optional mit Link-Vorschau oder Bild."""
        return await li.create_post(text, visibility, article_url, article_title, article_description, image_path, image_alt_text)

    @tool(DESTRUCTIVE)
    async def linkedin_delete_post(
        post_urn: Annotated[str, Field(description="URN des Posts, z. B. urn:li:share:123 oder urn:li:ugcPost:123")],
    ) -> dict[str, Any]:
        """Löscht einen eigenen LinkedIn-Post."""
        return await li.delete_post(post_urn)

    # --- YouTube -----------------------------------------------------------------

    @tool(READ)
    async def youtube_get_channel() -> dict[str, Any]:
        """Kanalinfos und Statistiken (Abos, Aufrufe, Videoanzahl) des verbundenen YouTube-Kanals."""
        return await yt.get_channel()

    @tool(READ)
    async def youtube_list_my_videos(
        max_results: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> list[dict[str, Any]]:
        """Neueste Uploads des eigenen Kanals mit Titel, Beschreibung, Status und Statistiken."""
        return await yt.list_my_videos(max_results)

    @tool(READ)
    async def youtube_get_videos(
        video_ids: Annotated[list[str], Field(description="Bis zu 50 Video-IDs", min_length=1, max_length=50)],
    ) -> list[dict[str, Any]]:
        """Details und Statistiken (Aufrufe, Likes, Kommentare) zu bestimmten Videos."""
        return await yt.get_videos(video_ids)

    @tool(READ)
    async def youtube_list_comments(
        video_id: str,
        max_results: Annotated[int, Field(ge=1, le=100)] = 20,
        order: Literal["time", "relevance"] = "time",
    ) -> list[dict[str, Any]]:
        """Kommentare (inkl. Antworten) zu einem Video."""
        return await yt.list_comments(video_id, max_results, order)

    @tool(WRITE)
    async def youtube_post_comment(video_id: str, text: str) -> dict[str, Any]:
        """Schreibt einen neuen Kommentar unter ein Video (als der verbundene Kanal)."""
        return await yt.post_comment(video_id, text)

    @tool(WRITE)
    async def youtube_reply_to_comment(
        parent_comment_id: Annotated[str, Field(description="comment_id aus youtube_list_comments")],
        text: str,
    ) -> dict[str, Any]:
        """Antwortet auf einen YouTube-Kommentar."""
        return await yt.reply_to_comment(parent_comment_id, text)

    @tool(WRITE)
    async def youtube_update_video(
        video_id: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        privacy_status: Literal["public", "unlisted", "private"] | None = None,
    ) -> dict[str, Any]:
        """Ändert Titel, Beschreibung, Tags oder Sichtbarkeit eines eigenen Videos. Nicht angegebene Felder bleiben unverändert."""
        return await yt.update_video(video_id, title, description, tags, privacy_status)

    @tool(WRITE)
    async def youtube_upload_video(
        file_path: Annotated[str, Field(description="Lokaler Pfad zur Videodatei")],
        title: Annotated[str, Field(max_length=100)],
        description: Annotated[str, Field(max_length=5000)] = "",
        tags: list[str] | None = None,
        privacy_status: Annotated[Literal["public", "unlisted", "private"], Field(description="Standard: private - erst prüfen, dann freischalten")] = "private",
        category_id: Annotated[str, Field(description="YouTube-Kategorie-ID, 22 = People & Blogs, 28 = Science & Technology")] = "22",
        made_for_kids: bool = False,
    ) -> dict[str, Any]:
        """Lädt ein Video auf den eigenen YouTube-Kanal hoch."""
        return await yt.upload_video(file_path, title, description, tags, privacy_status, category_id, made_for_kids)

    # --- Instagram ---------------------------------------------------------------

    @tool(READ)
    async def instagram_get_profile() -> dict[str, Any]:
        """Profil und Kennzahlen (Follower, Beiträge) des verbundenen Instagram-Kontos."""
        return await ig.get_profile()

    @tool(READ)
    async def instagram_list_media(limit: Annotated[int, Field(ge=1, le=100)] = 12) -> list[dict[str, Any]]:
        """Neueste Beiträge mit Caption, Link, Likes und Kommentaranzahl."""
        return await ig.list_media(limit)

    @tool(READ)
    async def instagram_get_media_insights(
        media_id: str,
        metrics: Annotated[list[str] | None, Field(description="z. B. reach, likes, comments, saved, shares, total_interactions, views")] = None,
    ) -> dict[str, Any]:
        """Insights (Reichweite, Interaktionen, Aufrufe) zu einem Beitrag."""
        return await ig.get_media_insights(media_id, metrics)

    @tool(READ)
    async def instagram_list_comments(media_id: str, limit: Annotated[int, Field(ge=1, le=100)] = 50) -> list[dict[str, Any]]:
        """Kommentare (inkl. Antworten) zu einem Beitrag."""
        return await ig.list_comments(media_id, limit)

    @tool(WRITE)
    async def instagram_reply_to_comment(comment_id: str, text: str) -> dict[str, Any]:
        """Antwortet auf einen Kommentar unter einem eigenen Beitrag."""
        return await ig.reply_to_comment(comment_id, text)

    @tool(WRITE)
    async def instagram_publish_image(
        image_url: Annotated[str, Field(description="Öffentlich erreichbare HTTPS-URL eines JPEG-Bildes")],
        caption: Annotated[str, Field(max_length=2200)] = "",
        alt_text: str | None = None,
    ) -> dict[str, Any]:
        """Veröffentlicht ein Bild als Feed-Beitrag."""
        return await ig.publish_image(image_url, caption, alt_text)

    @tool(WRITE)
    async def instagram_publish_carousel(
        image_urls: Annotated[list[str], Field(description="2-10 öffentliche HTTPS-URLs von JPEG-Bildern", min_length=2, max_length=10)],
        caption: Annotated[str, Field(max_length=2200)] = "",
    ) -> dict[str, Any]:
        """Veröffentlicht ein Karussell aus mehreren Bildern."""
        return await ig.publish_carousel(image_urls, caption)

    @tool(WRITE)
    async def instagram_publish_reel(
        video_url: Annotated[str, Field(description="Öffentlich erreichbare HTTPS-URL eines MP4-Videos")],
        caption: Annotated[str, Field(max_length=2200)] = "",
        share_to_feed: bool = True,
    ) -> dict[str, Any]:
        """Veröffentlicht ein Reel. Die Verarbeitung durch Instagram kann einige Minuten dauern."""
        return await ig.publish_reel(video_url, caption, share_to_feed)

    return mcp
