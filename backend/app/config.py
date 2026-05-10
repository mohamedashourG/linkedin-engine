from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["dev", "prod"] = "dev"
    app_url: str = "http://localhost:3000"
    api_url: str = "http://localhost:8000"

    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db: str = "linkedin_engine"

    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "change-me-in-prod"
    jwt_algorithm: str = "HS256"
    jwt_expires_hours: int = 720

    rule_23_pepper: str = "change-me-in-prod-and-rotate"
    # When true, daily_run seals without RULE 23 floor / cofounder / comment checks.
    # Use SKIP_RULE_23=true for local debugging; keep false in production.
    skip_rule_23: bool = False

    openai_api_key: str = ""
    openai_model_primary: str = "gpt-5.4"
    openai_model_cheap: str = "gpt-5.4-mini"

    azure_openai_key: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_version: str = "2025-04-01-preview"
    azure_openai_deployment_primary: str = "gpt-5.4"
    azure_openai_deployment_cheap: str = "gpt-4.1"

    apidirect_api_key: str = ""
    apidirect_mock: bool = False

    # Discovery source toggles. Disable any source by setting to false; the
    # discovery stage skips it entirely without erroring.
    discovery_use_apidirect: bool = True
    discovery_use_crustdata: bool = True
    discovery_use_unipile: bool = True
    discovery_use_exa: bool = True
    # RULE 24 — opt-out flag for the people-search channel. Defaults on but
    # can be flipped per-tenant via env (DISCOVERY_USE_TITLE_SEARCH=false) if
    # account safety becomes a concern.
    discovery_use_title_search: bool = True
    # Posts older than this are dropped at verification — keeps the slate
    # fresh and avoids wasting enrichment credits on stale content.
    # Candidates with no published_at are kept (we don't penalize missing
    # data). Set to 0 to disable the recency filter.
    discovery_max_age_days: int = 14
    # Apidirect: fetch this many pages per keyword (page=1..N, ~20 posts/page).
    discovery_apidirect_max_pages: int = 4
    # Contact-seed name searches: pages per contact on apidirect.
    discovery_contact_seed_apidirect_pages: int = 4
    # Contact-seed Unipile direct path: posts pulled per contact via
    # `unipile.get_user_posts`. These are written with status="gate_passed"
    # and bypass the entire verification + 4-gate funnel.
    discovery_contact_unipile_posts_per_user: int = 20
    # Drop posts older than this from contact-direct fetches. The bypass
    # path skips verification's recency gate, so this enforces freshness
    # locally. 90d ≈ 3 months. Set 0 to disable. Posts with no parseable
    # date are KEPT (not penalized for missing data).
    discovery_contact_unipile_max_age_days: int = 90
    # When True, each contact-direct post is run through the post_quality
    # gate (cheap LLM, ~$0.0005/call) to drop recruiting ads, event
    # announcements, vague platitudes, and other low-signal content even
    # though the rest of the gate funnel is bypassed. The synthetic
    # `gate_results.post_quality` then carries the real verdict instead of
    # the synthetic "operator_curated_contact" placeholder.
    discovery_contact_unipile_run_post_quality: bool = True
    # When product_extracted.target_geographies (or ICP geography tiers) is
    # non-empty, drop verified candidates whose post + author text does not
    # mention any geography term (substring match, case-insensitive).
    discovery_require_geo_in_post: bool = True
    # Clear Exa circuit at the start of each discover_for_operator so a fixed
    # API key takes effect without restarting Celery (set false to keep circuit latched).
    exa_reset_circuit_each_discovery: bool = True

    exa_api_key: str = ""
    exa_results_per_query: int = 100

    pdl_api_key: str = ""
    # Enrichment toggles. Profile-resolve runs Crustdata first when
    # enrich_with_crustdata=true (batched title + employer + headline).
    # When enrich_with_pdl=true, PDL runs after Crustdata (supplement) or alone
    # if Crustdata is off: job_title_levels (seniority) plus gap-fill for
    # title/company/name. Enable both for best ICP signals.
    enrich_with_pdl: bool = False
    enrich_with_crustdata: bool = True

    unipile_api_key: str = ""
    unipile_subdomain: str = ""
    unipile_port: int = 443
    unipile_mock: bool = False

    resend_api_key: str = ""
    resend_from_email: str = "hello@example.com"

    calendly_webhook_base_url: str = "http://localhost:8000/api/webhooks/calendly"

    crustdata_api_key: str = ""
    crustdata_webhook_base_url: str = ""
    crustdata_webhook_secret: str = "change-me-crustdata-hmac"
    crustdata_default_expiration_days: int = 365
    crustdata_inbox_lookback_hours: int = 48

    cookie_secure: bool = False
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cookie_domain: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
