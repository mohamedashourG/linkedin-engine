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

    # Bright Data — LinkedIn dataset scrape API. Async snapshot model: POST
    # /datasets/v3/scrape returns a snapshot_id; poll /snapshot/{id} until
    # status="ready". Used as a (planned) first-layer keyword discovery
    # source before apidirect. Set BRIGHTDATA_API_KEY to enable.
    brightdata_api_key: str = ""
    # Default dataset is LinkedIn job listings (gd_lpfll7v5hcqtkxl6l). Override
    # per-call when calling submit_search_request.
    brightdata_jobs_dataset_id: str = "gd_lpfll7v5hcqtkxl6l"
    # Snapshot polling. Bright Data scrapes typically resolve in 2-10 min;
    # keep timeout generous but bounded so a stuck snapshot doesn't hang
    # the whole discovery pass.
    brightdata_poll_timeout_s: int = 600
    brightdata_poll_interval_s: int = 10
    discovery_use_brightdata: bool = False

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
    # When Crustdata is on and the inbox drain inserts 0 rows, POST the
    # simulation watch once so api.crustdata.com responds and (per their API)
    # can immediately POST a sample payload to the cofounder webhook.
    discovery_crustdata_simulation_ping_on_empty_inbox: bool = True
    # Crustdata screener: synchronous POST /screener/linkedin_posts/keyword_search/
    # (posts in the HTTP response). Runs when discovery_use_crustdata is true.
    discovery_use_crustdata_screener: bool = True
    discovery_crustdata_screener_max_keyword_calls: int = 5
    discovery_crustdata_screener_limit_per_keyword: int = 8
    discovery_crustdata_screener_date_posted: str = "past-month"
    # Unipile keyword post search filters (POST /linkedin/search, classic).
    # All server-side; trim noise BEFORE we burn LLM gate cost on it.
    #   sort_by       "relevance" | "date" (newest first)
    #   date_window   "" runs past_day → past_week → past_month per keyword;
    #                 or set one value e.g. past_week only (see Unipile docs).
    #   content_type  e.g. "documents" (empty = omit from body)
    #   author_filter boolean string against the author headline,
    #                 e.g., "CEO OR VP OR Founder OR Director". Empty = no filter.
    discovery_unipile_post_sort_by: str = "relevance"
    discovery_unipile_post_date_window: str = ""
    discovery_unipile_post_content_type: str = "documents"
    discovery_unipile_post_author_filter: str = ""
    # Results per GET page (/linkedin/search?limit=&cursor=).
    discovery_unipile_post_limit: int = 20
    # Cursor-pagination depth for each date window. Each page is one POST;
    # clamped 1-10. With default empty date_window, up to 3 windows × max_pages.
    discovery_unipile_post_max_pages: int = 3
    # LinkedIn location geo IDs for classic POST /linkedin/search (category=posts).
    # Comma-separated digit strings (e.g. "103644278" for US). When empty and
    # discovery_unipile_post_resolve_location is true, discovery resolves the first
    # ICP geography string via GET /linkedin/search/parameters?type=LOCATION.
    discovery_unipile_post_location_ids: str = ""
    discovery_unipile_post_resolve_location: bool = True
    # When true (default), drop LinkedIn company-authored posts from Unipile
    # keyword and title-search activity paths (person buyers only).
    discovery_unipile_skip_company_posts: bool = True
    # Inline author enrichment + per-operator rubric inside _run_unipile.
    # Reference: unipile_hybrid_sweep.py (hybrid_sweep + Client 2 scripts).
    # When enabled, every post returned by Unipile keyword search is:
    #   1. Author-enriched via free /users/{slug} call (cached cross-run)
    #   2. Scored: title (5) + industry (3) + geo (2) against operator ICP
    #   3. Post text scored against operator's tier 1/2/3 keyword pool
    #   4. Qualified dual-path; non-qualifiers dropped before insert
    discovery_unipile_inline_rubric_enabled: bool = True
    # Path A: enriched author rubric ≥ this passes (default 6 — needs at
    # least title + geo, or industry + geo + something).
    discovery_unipile_inline_path_a_threshold: int = 6
    # Path B: post-text keyword relevance ≥ this passes (default 3 —
    # one tier_1 hit, or two tier_2 hits).
    discovery_unipile_inline_path_b_threshold: int = 3
    # Require an author-location geo hit on every qualifying candidate
    # (when the operator has target_geographies set). Disables only the
    # geo gate; the rubric thresholds still apply.
    discovery_unipile_inline_require_geo: bool = True
    # Cap on /users/{slug} profile fetches per cofounder per run (cache
    # hits don't count). Protects the LinkedIn account from per-keyword
    # spike. Reference scripts use 80; we default to 100 to keep ICPs with
    # bigger keyword pools fully covered.
    discovery_unipile_max_profile_fetches_per_run: int = 100
    # Days a cached profile is reused before refetching.
    discovery_unipile_author_cache_ttl_days: int = 14
    # 14-day no-repeat ledger for keywords (RULE 15) is too aggressive for
    # tenants with small keyword pools (~10-20 kws): they exhaust after one
    # run and sit idle for two weeks. 3 days lets a small pool cycle weekly
    # while still preventing same-day repeats. Override per-tenant via
    # KEYWORD_HISTORY_LOOKBACK_DAYS. Ignored when discovery_keyword_history_enabled
    # is false.
    keyword_history_lookback_days: int = 3
    # When false (default), the same keyword may run on every pass: filter_unused
    # returns the full list and mark_used is a no-op. Set true to re-enable the
    # RULE 15 / RULE 24 per-operator, per-channel no-repeat ledger in Mongo.
    discovery_keyword_history_enabled: bool = False
    # Posts older than this are dropped at verification — keeps the slate
    # fresh and avoids wasting enrichment credits on stale content.
    # Candidates with no published_at are kept (we don't penalize missing
    # data). Set to 0 to disable the recency filter.
    discovery_max_age_days: int = 14
    # Apidirect: fetch this many pages per keyword (page=1..N, ~20 posts/page).
    discovery_apidirect_max_pages: int = 4
    # After each post from GET /v1/linkedin/posts, call GET /v1/linkedin/post
    # per URL (~$0.002/call) for full text, author profile URL, URN, etc.
    discovery_apidirect_fetch_post_details: bool = False
    # Adds sentiment to post-details responses (+$0.001/call per vendor docs).
    discovery_apidirect_post_details_get_sentiment: bool = False
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
    # Default off: contact-only Unipile is operator-curated; pass posts through
    # to drafting without an extra LLM drop step.
    discovery_contact_unipile_run_post_quality: bool = False
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
    # Watcher registration: Crustdata rejects long OR-chains (boolean op cap) and
    # free-form ICP industry labels; see app.routes.crustdata._spec_from_operator.
    crustdata_watch_max_boolean_operators: int = 5
    crustdata_watch_default_headcount_buckets: list[str] = Field(
        default_factory=lambda: ["201-500", "501-1,000", "1,001-5,000"]
    )
    # Log full Crustdata webhook JSON at INFO (large). Auto-enabled when APP_ENV=dev;
    # set true in prod only while debugging; false suppresses even in dev.
    crustdata_log_full_webhook_payload: bool = False

    cookie_secure: bool = False
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cookie_domain: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
