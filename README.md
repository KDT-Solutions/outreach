# Outreach

Self-hosted, multi-tenant B2B cold-email outreach for Swiss SMEs. Replaces an
"AutoGTM"-style SaaS with four plain building blocks:

1. **Lead sourcing** – Google Places (New) text search per "industry + town" query,
   deduplicated per tenant and domain.
2. **Enrichment** – crawls the company website (home, Impressum, Kontakt), extracts
   e-mail addresses, falls back to Hunter.io domain search, checks the MX record.
3. **Copy** – Claude writes subject and body per lead and step, reads the contact
   person out of the Impressum, and builds the salutation. Drafts land in a review
   queue unless the campaign is set to auto-approve.
4. **Sending & replies** – SMTP2GO per tenant, business-hours window, daily cap,
   jitter, one-click unsubscribe (RFC 8058), bounce/spam/unsubscribe webhooks,
   reply detection through the tenant's Zammad (the reply-to mailbox is a Zammad channel): new tickets are matched to leads, classified, the sequence stops, and the lead context lands as an internal note on the ticket.

## Stack

FastAPI + SQLAlchemy + Postgres, a worker process, server-rendered UI. Three
containers: `app`, `worker`, `db`.

## Running

```
cp .env.example .env   # fill in the values
docker compose up -d   # image comes from ghcr.io, built by the GitHub Actions workflow
```

The UI listens on `HOST_PORT` (default 8429, bound to `BIND_ADDR`, default loopback). Put a reverse proxy with TLS in
front and set `PUBLIC_BASE_URL` to the public URL – it is used for the
unsubscribe links and the CSRF origin check.

Generate the secrets:

```
openssl rand -hex 32                                                   # SECRET_KEY, API_TOKEN
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # DATA_ENCRYPTION_KEY
```

### External services

| Service | Needed for | Where it goes |
|---|---|---|
| Google Cloud project with **Places API (New)** enabled, API key restricted to that API | Lead sourcing | `GOOGLE_PLACES_API_KEY` |
| Anthropic API key | Copywriting, reply classification | `ANTHROPIC_API_KEY` |
| Hunter.io API key (optional, free plan is enough) | E-mail fallback | `HUNTER_API_KEY` |
| SMTP2GO API key, sending domains verified (SPF/DKIM) | Sending | `SMTP2GO_API_KEY` globally, or per tenant in the UI (tenant key wins) |
| Zammad URL + API token (optional) | Reply detection | Tenant settings; the token needs ticket.agent rights |

Google Places pricing: Text Search Pro has a free monthly quota (5'000 calls at the
time of writing), Place Details Enterprise (website + phone) 1'000. The tool only
calls details for places it has not seen before.

### SMTP2GO webhook

In SMTP2GO, add a webhook for `bounce`, `spam` and `unsubscribe` events pointing to
the URL shown on the tenant page:

```
https://<PUBLIC_BASE_URL>/webhooks/smtp2go/<tenant-slug>/<webhook-token>
```

## Workflow

1. Create a tenant (sender identity, SMTP2GO key, what the company sells, signature,
   legal footer, daily limit, send window).
2. Create a campaign: search queries, target group, offer, sequence steps.
   Activate it. The worker sources leads within one interval.
3. Leads get enriched and drafted. Review drafts under **Freigabe** (edit, approve,
   regenerate, discard) or enable auto-approve on the campaign.
4. Approved mails go out inside the send window, respecting the daily cap.
   Follow-ups are drafted a day before they are due.
5. Replies arrive in Zammad; the worker polls new tickets every two minutes, stops the sequence, classifies the reply and notes the lead context on the ticket.

## JSON API

All `/api/...` routes need `Authorization: Bearer <API_TOKEN>`:

- `GET /api/tenants`, `GET /api/campaigns`
- `GET /api/leads?campaign_id=&status=`, `GET /api/leads/{id}`
- `POST /api/messages/{id}/approve` (optional JSON `{"subject","body"}`)
- `POST /api/leads/{id}/stop`
- `POST /api/campaigns/{id}/source`

## API und MCP

Für Automatisierung ohne Browser-Login gibt es einen eigenen Dienst `outreach-mcp`
(gleiches Image, eigener Container). Er arbeitet direkt auf der Datenbank und der
Versandlogik von `app` und `worker`, es gibt keine zweite Datenhaltung.

| Pfad | Inhalt |
|---|---|
| `/api/v1/...` | REST, JSON |
| `/api/v1/openapi.json` | OpenAPI-Beschreibung (braucht ebenfalls den Token) |
| `/mcp` | MCP-Server, Streamable HTTP |

Die ältere JSON-API unter `/api/...` (Abschnitt oben, `API_TOKEN`) bleibt unverändert im `app`-Container.

### Einrichtung

1. Token erzeugen (mindestens 32 Zeichen) und als Stack-Variable `OUTREACH_API_TOKEN` hinterlegen,
   in Portainer unter den Umgebungsvariablen des Stacks oder lokal in `.env`. Nie ins Repo.

   ```
   openssl rand -hex 32
   ```

2. Mandanten zuordnen: `OUTREACH_API_TENANTS=slug-a,slug-b` (Slugs aus der Oberfläche, `*` = alle,
   leer = keiner). Zugriffe auf andere Mandanten liefern 404.
3. Optional: `OUTREACH_API_ALLOW_IPS` (IPs oder CIDRs, kommagetrennt, leer = alle),
   `OUTREACH_API_RATE_LIMIT` (Anfragen pro Minute und Token, Standard 60),
   `OUTREACH_TEST_ALLOW_DOMAINS` (Domains, an die `send_test` senden darf; leer = `send_test` gesperrt).
4. `docker compose up -d outreach-mcp`. Der Dienst lauscht auf `MCP_HOST_PORT` (Standard 8430,
   gebunden an `BIND_ADDR`) und startet nicht, wenn der Token fehlt oder zu kurz ist.
5. Reverse Proxy (z.B. Nginx Proxy Manager) für `outreach.example.com`: SSL erzwingen, Custom
   Locations `/api/v1` und `/mcp` auf `<host>:8430` zeigen lassen; alles andere geht weiter an `app`.
   Der Dienst lehnt Anfragen ab, die nicht über HTTPS kamen (`X-Forwarded-Proto`), und nimmt auf `/mcp`
   nur den Host aus `PUBLIC_BASE_URL` an (abweichend: `OUTREACH_MCP_ALLOWED_HOSTS`).
6. Mit IP-Allowlist zusätzlich `MCP_FORWARDED_ALLOW_IPS` auf die IP des Proxys setzen, damit nur
   der Proxy die Client-IP (`X-Forwarded-For`) setzen darf.

Jeder Aufruf braucht `Authorization: Bearer <token>`. Ohne gültigen Token kommt `401` ohne Details.
Fehler sind immer JSON:

```json
{"error": {"code": "validation_error", "message": "Ungültige Eingabe", "details": [{"loc": ["leads", 0, "email"], "msg": "Ungültige E-Mail-Adresse"}]}}
```

### Werkzeuge

| MCP-Werkzeug | REST |
|---|---|
| `list_tenants` | `GET /api/v1/tenants` |
| `list_campaigns` | `GET /api/v1/campaigns?tenant=` |
| `get_campaign` | `GET /api/v1/campaigns/{id}` |
| `create_campaign` | `POST /api/v1/campaigns` |
| `import_leads` | `POST /api/v1/campaigns/{id}/leads/import` |
| `list_leads` | `GET /api/v1/campaigns/{id}/leads?status=&limit=&offset=` |
| `update_lead` | `PATCH /api/v1/leads/{id}` |
| `delete_lead` | `DELETE /api/v1/leads/{id}?confirm=true` (Soft-Delete) |
| `set_sequence` | `PUT /api/v1/campaigns/{id}/sequence` |
| `preview_email` | `GET /api/v1/campaigns/{id}/preview?lead_id=&step=` |
| `send_test` | `POST /api/v1/campaigns/{id}/send-test` |
| `start_campaign` / `pause_campaign` / `stop_campaign` | `POST /api/v1/campaigns/{id}/start` / `pause` / `stop` |
| `get_stats` | `GET /api/v1/campaigns/{id}/stats` |

Regeln:

- Schreibende Aufrufe liefern ohne `"confirm": true` nur eine Vorschau (`"confirmed": false`) und ändern
  nichts. `start_campaign` verlangt zusätzlich `"confirm_send": true`.
- Jeder schreibende Aufruf, auch eine Vorschau oder ein Fehler, landet in der Tabelle `api_audit`
  (Zeit, Token-Kennung `tok_…`, Mandant, Werkzeug, betroffene IDs, Ergebnis).
- Schritte sind 1-basiert. `delay_days` zählt ab dem vorherigen Versand, der erste Schritt hat 0.
- `body_text` ist Klartext. Erlaubte Platzhalter in Betreff und Text: `{{anrede}}`, `{{vorname}}`,
  `{{nachname}}`, `{{firma}}`, `{{ort}}`, `{{email}}`. `anrede` wird unverändert eingesetzt
  (z.B. "Guten Tag Herr Muster"). Signatur, Fusszeile und Abmeldelink hängt der Versand an.
- Bleibt ein Platzhalter beim Rendern leer, geht die Mail in die Freigabe, auch bei `auto_approve`.
- Import: Duplikate (gleiche E-Mail beim Mandanten, auch gelöschte Leads, doppelt in der Liste oder auf
  der Sperrliste) werden unter `skipped` gemeldet und nicht angelegt. Ungültige Zeilen lehnen den ganzen
  Import ab. Importierte Leads bekommen vor dem Versand eine MX-Prüfung durch den Worker.
- `auto_approve` beim Anlegen der Kampagne entscheidet, ob gerenderte Mails ohne Freigabe in der
  Oberfläche gesendet werden (Standard: nein).
- Antworten in `get_stats` stammen aus dem Zammad-Abgleich des Workers (`replies_source.last_poll`).

### Beispiele mit curl

```
API=https://outreach.example.com/api/v1
AUTH="Authorization: Bearer <token>"

# Leads importieren: erst Vorschau, dann ausführen
curl -sS -X POST "$API/campaigns/12/leads/import" -H "$AUTH" -H "Content-Type: application/json" -d '{
  "leads": [
    {"firma": "Muster AG", "anrede": "Guten Tag Herr Muster", "vorname": "Hans", "nachname": "Muster",
     "email": "hans.muster@example.org", "ort": "Bern", "sprache": "de", "notiz": "Messe 2026"}
  ],
  "confirm": true
}'

# Sequenz setzen
curl -sS -X PUT "$API/campaigns/12/sequence" -H "$AUTH" -H "Content-Type: application/json" -d '{
  "steps": [
    {"subject": "Kurze Frage an {{firma}}", "body_text": "{{anrede}}\n\n...", "delay_days": 0},
    {"subject": "Nachgefragt", "body_text": "{{anrede}}\n\n...", "delay_days": 4}
  ],
  "confirm": true
}'

# Fertige Mail für Lead 345, Schritt 1, ohne Versand
curl -sS "$API/campaigns/12/preview?lead_id=345&step=1" -H "$AUTH"

# Starten: Vorschau, dann mit beiden Bestätigungen
curl -sS -X POST "$API/campaigns/12/start" -H "$AUTH" -H "Content-Type: application/json" -d '{}'
curl -sS -X POST "$API/campaigns/12/start" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"confirm": true, "confirm_send": true}'
```

### MCP-Client

Claude Code:

```
claude mcp add --transport http outreach https://outreach.example.com/mcp --header "Authorization: Bearer <token>"
```

Clients mit JSON-Konfiguration:

```json
{
  "mcpServers": {
    "outreach": {
      "type": "http",
      "url": "https://outreach.example.com/mcp",
      "headers": {"Authorization": "Bearer <token>"}
    }
  }
}
```

### Tests

```
pip install -r requirements-dev.txt
python -m pytest -q
```

## Security notes

- Single admin login; signed, `HttpOnly`, `SameSite=Strict` session cookie; CSRF token
  on every form; login attempts rate-limited per IP.
- Tenant secrets (SMTP2GO, Zammad) are encrypted at rest with
  `DATA_ENCRYPTION_KEY`.
- Webhook URLs carry a per-tenant random token; unsubscribe links a per-lead token.
- The crawler fetches only the lead's own website (same host), max ~1.5 MB per page,
  four contact pages at most.
- No outbound mail leaves the system without an approved message, an address that
  passed the MX check, and a clear suppression list.

## Legal

Swiss UWG Art. 3 Abs. 1 lit. o restricts mass advertising mail. Keep the mails
individual, name the sender with postal address (tenant footer), and keep the
unsubscribe link – both are enforced by the sender. This is not legal advice.
