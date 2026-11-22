"""Settings, token storage and shared HTTP helpers."""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2 as httpx
from mcp.server.mcpserver.exceptions import ToolError

CONFIG_DIR = Path(os.environ.get("SOCIAL_MCP_HOME", Path.home() / ".config" / "social-mcp"))


def load_dotenv() -> None:
    """Load KEY=VALUE pairs from ./.env and the config dir without overriding real env vars."""
    for path in (Path.cwd() / ".env", CONFIG_DIR / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _default_linkedin_version() -> str:
    # LinkedIn publishes a versioned API every month and supports each for ~1 year.
    # Two months back is always released and still supported.
    today = dt.date.today()
    month = today.month - 2
    year = today.year
    if month <= 0:
        month += 12
        year -= 1
    return f"{year}{month:02d}"


@dataclass
class Settings:
    token_file: Path = field(default_factory=lambda: Path(os.environ.get("SOCIAL_MCP_TOKEN_FILE", CONFIG_DIR / "tokens.json")))
    redirect_uri: str = field(default_factory=lambda: os.environ.get("SOCIAL_MCP_REDIRECT_URI", "http://localhost:8765/callback"))

    linkedin_client_id: str = field(default_factory=lambda: os.environ.get("LINKEDIN_CLIENT_ID", ""))
    linkedin_client_secret: str = field(default_factory=lambda: os.environ.get("LINKEDIN_CLIENT_SECRET", ""))
    linkedin_api_version: str = field(default_factory=lambda: os.environ.get("LINKEDIN_API_VERSION") or _default_linkedin_version())

    google_client_id: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLIENT_ID", ""))
    google_client_secret: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLIENT_SECRET", ""))

    instagram_app_id: str = field(default_factory=lambda: os.environ.get("INSTAGRAM_APP_ID", ""))
    instagram_app_secret: str = field(default_factory=lambda: os.environ.get("INSTAGRAM_APP_SECRET", ""))
    instagram_api_version: str = field(default_factory=lambda: os.environ.get("INSTAGRAM_API_VERSION", "v23.0"))


class TokenStore:
    """JSON file with one entry per platform, readable only by the current user."""

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, platform: str) -> dict[str, Any] | None:
        return self._read().get(platform)

    def set(self, platform: str, data: dict[str, Any]) -> None:
        all_tokens = self._read()
        all_tokens[platform] = data
        self._write(all_tokens)

    def delete(self, platform: str) -> bool:
        all_tokens = self._read()
        if all_tokens.pop(platform, None) is None:
            return False
        self._write(all_tokens)
        return True

    def _write(self, all_tokens: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tokens-")
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(all_tokens, fh, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def expires_at(expires_in: Any) -> float | None:
    return time.time() + float(expires_in) if expires_in else None


class ApiError(ToolError):
    """A platform or setup error whose message is shown to Claude as the tool result."""


def raise_for_status(resp: httpx.Response, platform: str) -> None:
    if resp.is_success:
        return
    detail: Any = resp.text
    try:
        body = resp.json()
        err = body.get("error", body)
        if isinstance(err, dict):
            detail = err.get("message") or err.get("error_description") or err
        else:
            detail = body.get("error_description") or body.get("message") or err
    except ValueError:
        pass
    hint = ""
    if resp.status_code == 401:
        hint = f" Token abgelaufen oder ungültig - bitte `social-mcp auth {platform}` erneut ausführen."
    raise ApiError(f"{platform} API {resp.status_code}: {detail}.{hint}")
