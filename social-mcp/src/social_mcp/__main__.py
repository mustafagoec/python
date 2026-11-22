"""Command line: `social-mcp serve | auth <platform> | status | logout <platform>`."""

from __future__ import annotations

import argparse
import sys
import time

from .config import ApiError, Settings, TokenStore, load_dotenv

PLATFORMS = ("linkedin", "youtube", "instagram")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="social-mcp", description="MCP-Server für LinkedIn, YouTube und Instagram")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="MCP-Server starten (Standard)")
    serve.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")

    auth = sub.add_parser("auth", help="Konto per OAuth verbinden")
    auth.add_argument("platform", choices=PLATFORMS)
    auth.add_argument("--token", help="Nur Instagram: Long-Lived-Token aus dem Meta App Dashboard direkt speichern")

    sub.add_parser("status", help="Verbundene Konten anzeigen")

    logout = sub.add_parser("logout", help="Gespeicherten Token löschen")
    logout.add_argument("platform", choices=PLATFORMS)

    args = parser.parse_args(argv)
    settings = Settings()
    store = TokenStore(settings.token_file)

    try:
        if args.command in (None, "serve"):
            from .server import create_server

            create_server(settings).run(getattr(args, "transport", "stdio"))
        elif args.command == "auth":
            from . import oauth

            if args.token:
                if args.platform != "instagram":
                    parser.error("--token wird nur für instagram unterstützt")
                tok = oauth.set_instagram_token(settings, store, args.token)
            else:
                tok = oauth.LOGINS[args.platform](settings, store)
            print(f"{args.platform} verbunden: {tok.get('account') or 'ok'} (gespeichert in {settings.token_file})")
        elif args.command == "status":
            for platform in PLATFORMS:
                tok = store.get(platform)
                if not tok:
                    print(f"{platform:10} nicht verbunden")
                    continue
                exp = tok.get("expires_at")
                days = f"{(exp - time.time()) / 86400:.0f} Tage" if exp else "unbekannt"
                print(f"{platform:10} {tok.get('account') or ''}  (Token gültig: {days})")
        elif args.command == "logout":
            print("gelöscht" if store.delete(args.platform) else "war nicht verbunden")
    except ApiError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
