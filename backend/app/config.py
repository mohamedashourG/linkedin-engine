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

    exa_api_key: str = ""
    exa_results_per_query: int = 100

    pdl_api_key: str = ""
    # Enrichment toggles. The author-resolution stage prefers Crustdata when
    # enrich_with_crustdata=true (3 credits/profile, returns title +
    # employer_name + headline), falls back to PDL when enrich_with_pdl=true
    # (~$0.20-$0.28/match, returns name only in practice), and becomes a
    # no-op when both are false (ICP scoring relies on LLM inference from
    # post text alone).
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
