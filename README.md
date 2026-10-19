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
