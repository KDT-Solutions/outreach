from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./outreach.db"
    secret_key: str
    data_encryption_key: str
    admin_user: str = "admin"
    admin_password: str
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

    tz: str = "Europe/Zurich"
    worker_interval_seconds: int = 60

    # crawler
    crawl_timeout: float = 12.0
    crawl_max_bytes: int = 1_500_000
    crawl_user_agent: str = "Mozilla/5.0 (compatible; OutreachBot/0.1; +https://example.com)"

    # batch sizes per worker tick
    enrich_batch: int = 15
    generate_batch: int = 10
    send_batch: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
