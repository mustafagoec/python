"""Interactive OAuth logins that store tokens for the MCP server.

Runs a one-shot HTTP server on the redirect URI, opens the browser, and exchanges the
returned code for tokens.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx2 as httpx

from . import instagram, linkedin, youtube
from .config import ApiError, Settings, TokenStore, expires_at, raise_for_status


def _wait_for_code(redirect_uri: str, authorize_url: str, state: str, timeout: float = 300) -> str:
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1"):
        print("Die Redirect-URI ist kein lokaler http-Endpunkt. Öffne diese URL im Browser:\n")
        print(authorize_url)
        url = input("\nNach dem Login die komplette URL aus der Adresszeile hier einfügen: ").strip()
        return _extract_code(url, state)

    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if urlparse(self.path).path != parsed.path:
                self.send_response(404)
                self.end_headers()
                return
            try:
                result["code"] = _extract_code(self.path, state)
                body, status = "Verbunden! Du kannst dieses Fenster schliessen.", 200
            except ApiError as exc:
                result["error"] = str(exc)
                body, status = f"Fehler: {exc}", 400
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    server.timeout = 1
    print(f"Öffne Browser für den Login ... falls nicht, diese URL aufrufen:\n{authorize_url}\n")
    threading.Thread(target=webbrowser.open, args=(authorize_url,), daemon=True).start()
    deadline = time.monotonic() + timeout
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "error" in result:
        raise ApiError(result["error"])
    if "code" not in result:
        raise ApiError("Kein Login innerhalb von 5 Minuten erhalten.")
    return result["code"]


def _extract_code(url: str, state: str) -> str:
    query = parse_qs(urlparse(url).query)
    if "error" in query:
        raise ApiError(query.get("error_description", query["error"])[0])
    if query.get("state", [None])[0] != state:
        raise ApiError("Ungültiger OAuth-State - Login bitte neu starten.")
    if "code" not in query:
        raise ApiError("Kein Autorisierungscode in der Redirect-URL.")
    # Instagram appends "#_" to the code.
    return query["code"][0].removesuffix("#_")


def _require(value: str, name: str) -> str:
    if not value:
        raise ApiError(f"{name} ist nicht gesetzt (siehe .env.example).")
    return value


def login_linkedin(settings: Settings, store: TokenStore) -> dict[str, Any]:
    client_id = _require(settings.linkedin_client_id, "LINKEDIN_CLIENT_ID")
    client_secret = _require(settings.linkedin_client_secret, "LINKEDIN_CLIENT_SECRET")
    state = secrets.token_urlsafe(24)
    url = linkedin.AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": settings.redirect_uri,
        "state": state, "scope": " ".join(linkedin.SCOPES),
    })
    code = _wait_for_code(settings.redirect_uri, url, state)
    resp = httpx.post(linkedin.TOKEN_URL, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": settings.redirect_uri,
        "client_id": client_id, "client_secret": client_secret,
    })
    raise_for_status(resp, "linkedin")
    data = resp.json()
    tok = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at": expires_at(data.get("expires_in")),
        "scope": data.get("scope"),
    }
    info = httpx.get("https://api.linkedin.com/v2/userinfo", headers={"Authorization": f"Bearer {tok['access_token']}"})
    raise_for_status(info, "linkedin")
    tok["person_urn"] = f"urn:li:person:{info.json()['sub']}"
    tok["account"] = info.json().get("name")
    store.set("linkedin", tok)
    return tok


def login_youtube(settings: Settings, store: TokenStore) -> dict[str, Any]:
    client_id = _require(settings.google_client_id, "GOOGLE_CLIENT_ID")
    client_secret = _require(settings.google_client_secret, "GOOGLE_CLIENT_SECRET")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    url = youtube.AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": settings.redirect_uri,
        "scope": " ".join(youtube.SCOPES), "state": state, "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    code = _wait_for_code(settings.redirect_uri, url, state)
    resp = httpx.post(youtube.TOKEN_URL, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": settings.redirect_uri,
        "client_id": client_id, "client_secret": client_secret, "code_verifier": verifier,
    })
    raise_for_status(resp, "youtube")
    data = resp.json()
    tok = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expires_at": expires_at(data.get("expires_in")),
        "scope": data.get("scope"),
    }
    ch = httpx.get(f"{youtube.API}/channels", params={"part": "snippet", "mine": "true"},
                   headers={"Authorization": f"Bearer {tok['access_token']}"})
    raise_for_status(ch, "youtube")
    items = ch.json().get("items") or [{}]
    tok["account"] = items[0].get("snippet", {}).get("title")
    store.set("youtube", tok)
    return tok


def _store_instagram_token(settings: Settings, store: TokenStore, access_token: str, expires_in: Any) -> dict[str, Any]:
    me = httpx.get(f"{instagram.GRAPH}/{settings.instagram_api_version}/me",
                   params={"fields": "user_id,username", "access_token": access_token})
    raise_for_status(me, "instagram")
    tok = {
        "access_token": access_token,
        "expires_at": expires_at(expires_in),
        "issued_at": time.time(),
        "user_id": me.json()["user_id"],
        "account": me.json().get("username"),
    }
    store.set("instagram", tok)
    return tok


def login_instagram(settings: Settings, store: TokenStore) -> dict[str, Any]:
    app_id = _require(settings.instagram_app_id, "INSTAGRAM_APP_ID")
    app_secret = _require(settings.instagram_app_secret, "INSTAGRAM_APP_SECRET")
    state = secrets.token_urlsafe(24)
    url = instagram.AUTHORIZE_URL + "?" + urlencode({
        "client_id": app_id, "redirect_uri": settings.redirect_uri, "response_type": "code",
        "scope": ",".join(instagram.SCOPES), "state": state,
    })
    code = _wait_for_code(settings.redirect_uri, url, state)
    short = httpx.post(instagram.SHORT_TOKEN_URL, data={
        "client_id": app_id, "client_secret": app_secret, "grant_type": "authorization_code",
        "redirect_uri": settings.redirect_uri, "code": code,
    })
    raise_for_status(short, "instagram")
    long = httpx.get(f"{instagram.GRAPH}/access_token", params={
        "grant_type": "ig_exchange_token", "client_secret": app_secret, "access_token": short.json()["access_token"],
    })
    raise_for_status(long, "instagram")
    data = long.json()
    return _store_instagram_token(settings, store, data["access_token"], data.get("expires_in"))


def set_instagram_token(settings: Settings, store: TokenStore, access_token: str) -> dict[str, Any]:
    """Store a long-lived token generated in the Meta App Dashboard (valid 60 days)."""
    return _store_instagram_token(settings, store, access_token, 60 * 24 * 3600)


LOGINS = {"linkedin": login_linkedin, "youtube": login_youtube, "instagram": login_instagram}
