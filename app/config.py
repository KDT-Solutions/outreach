from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./outreach.db"
    secret_key: str
    data_encryption_key: str
    admin_user: str = "admin"
    # argon2 hash of the admin password: python -m app.tools hash-password (base64-wrapped, because the
    # $ in a raw hash breaks .env files and Compose variable substitution)
    admin_password_hash: str = ""
    admin_password_hash_b64: str = ""
    # plain password; only used when no hash is set (logged as a warning at start-up)
    admin_password: str = ""
    # optional TOTP secret (base32) for a second factor: python -m app.tools totp-secret
    admin_totp_secret: str = ""
    api_token: str = ""
    public_base_url: str = "http://localhost:8000"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    google_places_api_key: str = ""
    hunter_api_key: str = ""
    # Global SMTP2GO key, used when a tenant has none of its own
    smtp2go_api_key: str = ""
    # Token for the shared SMTP2GO webhook URL (/webhooks/smtp2go/<token>)
    webhook_token: str = ""

    # Automation API (/api/v1 and MCP /mcp), served by the outreach-mcp service
    outreach_api_token: str = ""
    # alternative: path to a file holding the token (Docker secret)
    outreach_api_token_file: str = ""
    # comma-separated tenant slugs the token may access, "*" = all, empty = none
    outreach_api_tenants: str = ""
    # comma-separated IPs or CIDRs, empty = all
    outreach_api_allow_ips: str = ""
    outreach_api_rate_limit: int = 60  # requests per minute and token
    # reject requests that did not arrive over HTTPS (X-Forwarded-Proto from the proxy)
    outreach_api_require_https: bool = True
    # comma-separated Host header values the MCP endpoint accepts; default: host of PUBLIC_BASE_URL
    outreach_mcp_allowed_hosts: str = ""
    # comma-separated domains send_test may send to; empty = send_test disabled
    outreach_test_allow_domains: str = ""
    # comma-separated domains allowed as campaign sender/reply-to via the API, on top of the
    # domains of the tenant's own from/reply-to address; empty = tenant domains only
    outreach_sender_domains: str = ""
    # auto_approve=true via API/MCP (mails go out without review) only when this is true
    outreach_allow_auto_approve: bool = False

    tz: str = "Europe/Zurich"
    worker_interval_seconds: int = 60

    # crawler: per-page timeout and body limit; only public http(s) hosts on 80/443 are fetched
    crawl_timeout: float = 12.0
    crawl_max_bytes: int = 2_000_000
    crawl_user_agent: str = "Mozilla/5.0 (compatible; OutreachBot/0.1; +https://example.com)"

    # API search_leads / enrich_leads: per page timeout, pages per domain (incl. start
    # page), parallel lookups and a time budget for the whole call
    api_crawl_timeout: float = 10.0
    api_crawl_max_pages: int = 3
    api_prospect_workers: int = 8
    api_prospect_deadline_seconds: int = 240

    # batch sizes per worker tick
    enrich_batch: int = 15
    generate_batch: int = 10
    send_batch: int = 20


@lru_cache
def get_settings() -> Settings:
    s = Settings()  # type: ignore[call-arg]
    if s.admin_password_hash_b64 and not s.admin_password_hash:
        import base64

        s.admin_password_hash = base64.b64decode(s.admin_password_hash_b64).decode().strip()
    return s
