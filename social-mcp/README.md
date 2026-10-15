# social-mcp – Claude mit LinkedIn, YouTube und Instagram verbinden

Ein lokaler [MCP](https://modelcontextprotocol.io)-Server. Claude (Desktop, Code oder jeder andere MCP-Client)
bekommt damit Tools, um deine eigenen Konten zu lesen und – nach deiner Bestätigung – Inhalte zu veröffentlichen.

Der Server läuft auf deinem Rechner. Deine Tokens liegen nur lokal in `~/.config/social-mcp/tokens.json`
(Dateirechte `600`) und werden für YouTube und Instagram automatisch erneuert.

## Tools

| Plattform | Lesen | Schreiben |
|---|---|---|
| Allgemein | `social_status` | – |
| LinkedIn | `linkedin_get_profile` | `linkedin_create_post` (Text, Link-Vorschau oder Bild), `linkedin_delete_post` |
| YouTube | `youtube_get_channel`, `youtube_list_my_videos`, `youtube_get_videos`, `youtube_list_comments` | `youtube_post_comment`, `youtube_reply_to_comment`, `youtube_update_video`, `youtube_upload_video` |
| Instagram | `instagram_get_profile`, `instagram_list_media`, `instagram_get_media_insights`, `instagram_list_comments` | `instagram_reply_to_comment`, `instagram_publish_image`, `instagram_publish_carousel`, `instagram_publish_reel` |

Schreibende Tools sind im MCP-Protokoll als solche markiert, Claude fragt also vor jedem Post nach.

### Was die Plattformen *nicht* erlauben

- **LinkedIn:** Mit der Self-Service-API kannst du posten und dein Profil lesen. Eigene Posts, Feed,
  Kommentare oder Statistiken auslesen geht nur mit freigeschalteten Partnerprogrammen
  (Community Management API) – das ist hier deshalb nicht eingebaut.
- **Instagram:** Nur **Business- oder Creator-Konten** (kein privates Konto). Bilder und Videos müssen
  unter einer **öffentlichen HTTPS-URL** liegen – Instagram lädt sie von dort; lokale Dateien gehen nicht.
  Stories und DMs sind nicht enthalten.
- **YouTube:** Das Standard-Kontingent sind 10.000 Einheiten/Tag; ein Upload kostet ca. 1.600. Videos aus
  ungeprüften Google-Projekten werden von YouTube oft automatisch auf „privat“ gesetzt, bis die App ein
  Audit bestanden hat.

## Installation

```bash
cd social-mcp
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example ~/.config/social-mcp/.env           # oder .env im aktuellen Ordner
```

Python ≥ 3.10 und das MCP-SDK ≥ 2.2 werden benötigt.

## Entwickler-Apps anlegen (einmalig)

Für jede Plattform brauchst du eine eigene App, deren Zugangsdaten in die `.env` kommen.
Redirect-URI überall: **`http://localhost:8765/callback`** (änderbar über `SOCIAL_MCP_REDIRECT_URI`).

### LinkedIn
1. <https://www.linkedin.com/developers/apps> → *Create app* (braucht eine LinkedIn-Unternehmensseite, eine leere eigene reicht).
2. Tab **Products**: „Sign In with LinkedIn using OpenID Connect“ und „Share on LinkedIn“ hinzufügen.
3. Tab **Auth**: Redirect-URI eintragen, *Client ID* / *Client Secret* in `LINKEDIN_CLIENT_ID` / `LINKEDIN_CLIENT_SECRET`.
4. `social-mcp auth linkedin`

Der LinkedIn-Token gilt 60 Tage, danach `social-mcp auth linkedin` erneut ausführen.

### YouTube (Google Cloud)
1. <https://console.cloud.google.com> → neues Projekt → **YouTube Data API v3** aktivieren.
2. **OAuth-Zustimmungsbildschirm**: Typ „Extern“, dich selbst als Testnutzer eintragen.
   Tipp: auf „In Produktion“ stellen, sonst läuft der Refresh-Token nach 7 Tagen ab.
3. **Anmeldedaten → OAuth-Client-ID → Desktop-App**. ID/Secret in `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.
4. `social-mcp auth youtube` und den Kanal auswählen.

### Instagram
1. Instagram-Konto in den Einstellungen auf **Business** oder **Creator** umstellen.
2. <https://developers.facebook.com/apps> → *App erstellen* → Anwendungsfall „Mit Instagram-Nutzer*innen
   interagieren“ → **Instagram API mit Instagram-Login** einrichten.
3. Einfachster Weg: im Dashboard unter *Access-Token generieren* dein Konto hinzufügen, Token erzeugen und
   ```bash
   social-mcp auth instagram --token IGAA...
   ```
   Alternativ OAuth: Instagram-App-ID/-Secret in `INSTAGRAM_APP_ID` / `INSTAGRAM_APP_SECRET`, Redirect-URI
   eintragen und `social-mcp auth instagram`. Meta verlangt hier meist eine **https**-Redirect-URI – setze
   dann `SOCIAL_MCP_REDIRECT_URI` auf diese URL; das Tool fragt dich nach der Weiterleitung nach der
   kompletten URL aus der Adresszeile.

Der Token gilt 60 Tage und wird automatisch verlängert, solange du den Server mindestens alle ~7 Wochen nutzt.

Mit `social-mcp status` siehst du, welche Konten verbunden sind.

## In Claude einbinden

### Claude Desktop
`claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "social-media": {
      "command": "/ABSOLUTER/PFAD/social-mcp/.venv/bin/social-mcp",
      "args": ["serve"]
    }
  }
}
```

Unter Windows: `...\\.venv\\Scripts\\social-mcp.exe`. Danach Claude Desktop neu starten.

### Claude Code
```bash
claude mcp add social-media -- /ABSOLUTER/PFAD/social-mcp/.venv/bin/social-mcp serve
```

Die Zugangsdaten aus `~/.config/social-mcp/.env` werden beim Start automatisch geladen; alternativ
kannst du sie im `env`-Block der Claude-Konfiguration setzen.

## Beispiele für Prompts

- „Wie haben sich meine letzten 5 YouTube-Videos entwickelt? Welche Themen laufen am besten?“
- „Beantworte die unbeantworteten Kommentare unter meinem neuesten Video – zeig mir die Antworten vorher.“
- „Mach aus meinem neuesten YouTube-Video einen LinkedIn-Post mit Link.“
- „Poste dieses Bild https://… auf Instagram mit einer passenden Caption und 5 Hashtags.“

## Entwicklung

```bash
pip install -e ".[dev]"
pytest
```

Die Tests laufen komplett gegen gemockte APIs, ohne echte Konten.
