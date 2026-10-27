"""Environment-driven configuration.

All values come from environment variables (loaded from backend/.env if present).
Resolves relative paths against the `backend/` directory.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # backend/

try:
    from dotenv import load_dotenv
    # Look for .env in backend/ first, then fall back to project root
    _env_path = PROJECT_ROOT / ".env"
    if not _env_path.exists():
        _env_path = PROJECT_ROOT.parent / ".env"
    load_dotenv(_env_path)
except ImportError:
    pass


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"Env var {name} must be an integer, got {raw!r}")


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"Env var {name} must be a float, got {raw!r}")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def _path(name: str, default: str) -> Path:
    raw = os.getenv(name, default)
    p = Path(raw)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def _list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _lower_list(name: str, default: str) -> list[str]:
    """`_list` with everything lower-cased and de-duplicated (order kept).

    For word lists that are compared against normalised text — matching is
    always case-insensitive, so normalising once here means every caller does
    not have to remember to.
    """
    out: list[str] = []
    for x in _list(name, default):
        low = x.lower()
        if low not in out:
            out.append(low)
    return out


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    """Lower-cased env value, falling back to `default` when it is not one of
    `allowed` — for switches where a typo must fail closed rather than boot."""
    raw = (os.getenv(name) or "").strip().lower()
    return raw if raw in allowed else default


def _int_map(name: str, default: str) -> dict[str, int]:
    """`"reddit:15,kym:40"` -> `{"reddit": 15, "kym": 40}`.

    Malformed pairs are IGNORED rather than fatal: this is a per-source cap
    map, so a typo should cost that one source its override, never boot the
    whole backend. Last write wins on a duplicate key.
    """
    out: dict[str, int] = {}
    for pair in _list(name, default):
        key, sep, value = pair.partition(":")
        key = key.strip()
        if not sep or not key:
            continue
        try:
            out[key] = int(value.strip())
        except ValueError:
            continue
    return out


@dataclass(frozen=True)
class Config:
    # --- Postgres ---
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    db_sslmode: str
    db_pool_min: int
    db_pool_max: int
    # Per-connection statement_timeout (ms). Caps how long any single query can
    # hold a pooled connection — so a slow query can't drain the pool for 30s.
    # 0 disables. RAG vector queries degrade gracefully on timeout.
    db_statement_timeout_ms: int

    # --- Supabase ---
    supabase_project_ref: str
    supabase_url: str
    supabase_access_token: str

    # --- Scraper target ---
    target_url: str
    target_domain: str

    # --- HTTP ---
    user_agent: str
    request_timeout: int
    run_concurrency_fetch: int
    run_concurrency_openai: int

    # --- OpenAI ---
    openai_api_key: str
    openai_model_chat: str
    openai_model_vision: str
    openai_model_synthesis: str
    openai_model_embed: str
    openai_model_whisper: str
    openai_budget_usd: float
    # Who answers the calls the older agents make through `openai_client`:
    # "vertex" (Gemini, the default) or "openai". One switch for all of them.
    llm_provider: str

    # --- Local paths ---
    asset_root: Path
    frames_root: Path
    brand_root: Path
    upload_root: Path
    producer_root: Path

    # --- Vertex AI (Producer Agent) ---
    google_cloud_project: str
    google_cloud_location: str
    google_genai_use_vertexai: bool
    veo_output_bucket: str
    veo_model: str
    veo_model_premium: str
    veo_budget_usd: float
    veo_max_duration_sec: int
    veo_default_aspect: str
    # v5.1: per-script cost cap for the "Render all scenes" path. Pre-flight
    # blocks any submission whose estimated total exceeds this. With Veo Fast
    # at ~$0.40/scene plus optional $0.20/scene audio plus 3 scenes max, the
    # default $3 ceiling sits comfortably above a normal 3-scene+audio run
    # but kills any runaway "render 60 scenes" failure mode.
    producer_max_script_cost_usd: float
    vertex_gemini_model: str
    vertex_gemini_fast_model: str
    vertex_embed_model: str
    vertex_embed_dim: int
    vertex_image_model: str
    # The premium ("Nano Banana Pro") image tier, as a MODEL+LOCATION PAIR —
    # preview image models are published to one endpoint only, so swapping the
    # model without the location just 404s on the other axis. Access is granted
    # per GCP project, which is why this has to be configurable rather than a
    # constant: a project with no allow-listing gets a hard
    # `NOT_FOUND: Publisher model … was not found`.
    vertex_image_pro_model: str
    vertex_image_pro_location: str
    # Model gateway: regions to fail over to on RESOURCE_EXHAUSTED (quota 429),
    # a global in-flight cap so bursts smooth under the per-minute ceiling, and
    # the Claude-on-Vertex brain (separate quota pool, same ADC credentials).
    vertex_fallback_locations: list[str]
    vertex_max_concurrency: int
    claude_vertex_region: str
    claude_model: str
    # Fallback only — used iff Claude isn't enabled in the project's Vertex
    # Model Garden. Empty = use Claude-on-Vertex (the default, no extra key).
    anthropic_api_key: str
    anthropic_model: str
    # --- Video-editor agent (services/model_gateway.py) ---
    # "" / "auto" probes vertex-claude -> anthropic-claude -> gemini and uses the
    # first that answers. Pin one of those names to force it.
    ve_agent_provider: str
    # Gemini runs the SAME tool loop when no Claude is reachable, so the editor
    # is agentic on every deployment. Pro, because this is the reasoning seat.
    ve_agent_gemini_model: str
    # Hard ceiling on tool round-trips in one agent run.
    ve_agent_max_steps: int

    # --- Remotion compose service ---
    remotion_base_url: str
    remotion_default_crossfade_sec: float

    # --- Instagram ingest (Agent 5) ---
    instagram_access_token: str
    instagram_account_id: str
    instagram_target_username: str
    instagram_api_version: str
    instagram_root: Path
    instagram_max_videos_per_run: int

    # --- General trends (Trends page → "General trends" tab) ---
    # THE MASTER SWITCH, and it is OFF. The feed ships but does not yet do what
    # it was built for: too many cards are things that HAPPENED rather than
    # formats people are RECREATING, which is a research problem in source
    # selection, not a bug with a patch. So the whole stream is paused until
    # that research is done — the poller does not run, "Refresh now" is refused,
    # and the social-listening report is built from the other two streams alone
    # rather than carrying a section nobody trusts.
    #
    # PAUSED, NOT DELETED. Every collector, parser, scorer, table and stored row
    # stays exactly where it is; `GENERAL_TRENDS_ENABLED=true` brings all of it
    # back with no other edit. The frontend has its own half of this switch in
    # `frontend/src/components/trends/featureFlags.ts` (a static build cannot
    # read this file) — flip BOTH or the tab reappears pointing at a collector
    # that is not collecting.
    # --- Background workers ---
    # Master switch for every background poller. ON by default, because the
    # deployed server is what runs them and a default of off would silently
    # stop all unattended work the first time someone forgot the variable.
    #
    # The reason it exists: the pollers are WORKERS, not observers. They write
    # rows, send reminder emails and spend Vertex/Suno credits. Since localhost
    # now points at the production database, a dev machine running `run.py` is a
    # second worker competing with the server for the same rows, and only 2 of
    # the 17 claim work with `FOR UPDATE SKIP LOCKED`. The other 15 can
    # duplicate the work outright: two Veo polls, two reminder emails, two
    # billable image generations.
    #
    # So: set `POLLERS_ENABLED=false` in a local .env, and leave it on (or
    # unset) wherever the app is actually deployed. docker-compose pins it to
    # true explicitly so a copied-down .env cannot disarm the server.
    pollers_enabled: bool
    # Competitor Instagram scans: the scan poller queues a scan of every active
    # account once the last one is this many hours old; 0 = the Scan button
    # only. Before it existed the feed froze on the last press while every
    # account kept posting (nothing was read 2026-09-18 to 2026-10-07).
    trend_auto_scan_hours: int
    general_trends_enabled: bool
    # Free public sources fetched every refresh (ids: google_trends, bluesky,
    # reddit, kym, wikipedia, youtube, hacker_news, lemmy, github, google_news,
    # tiktok, instagram, instagram_watch — plus x_trends, kept in the code but
    # no longer in the default list). youtube only runs when `youtube_api_key`
    # is set and the two instagram lanes only when the Graph token is live.
    # Regions drive Google Trends geo, YouTube regionCode and the trends24
    # slug; the rest are region "global".
    general_trends_sources: list[str]
    general_trends_geos: list[str]
    general_trends_subreddits: list[str]
    # Auto-refresh cadence for the poller; 0 = manual "Refresh now" only.
    general_trends_refresh_hours: int
    general_trends_max_items_per_source: int
    # Per-source override of the item cap, e.g. {"reddit": 15, "kym": 40}.
    # Sources absent from the map keep `general_trends_max_items_per_source`.
    general_trends_source_caps: dict[str, int]
    # Feed window: how far back `last_seen_at` may be for a card to show. This
    # answers "is a source still listing it" and was hard-coded (and therefore
    # unreachable from the API) at 14 days before.
    general_trends_feed_days: int
    # Content window: how old the newest EVIDENCE behind a card may be. This is
    # the owner's "nothing older than about a month" and is a different question
    # from the feed window — a trend first seen 3 weeks ago that is still
    # collecting fresh sightings stays.
    general_trends_max_content_age_days: int
    # THE PARTICIPATION GATE: which `trend_kind`s are allowed into the feed at
    # all. A trend is something people are RECREATING — a meme format, a sound,
    # a challenge — not a thing that merely happened. In the first live corpus
    # only 103 of 363 rows were a participatory kind; the other 260 were news,
    # events and culture that no social team could make their own version of.
    # Set to an empty string to show every kind.
    general_trends_kinds: list[str]
    # Regions for the YouTube Shorts-usage chart — the only geo-correct
    # participatory source available, and the main supply of `sound` trends.
    general_trends_shorts_regions: list[str]
    # "on" drops explicit/harmful items and flags bait/avoid topics at ingest,
    # "flag_only" flags instead of dropping (useful while tuning the lists),
    # "off" disables the screen entirely.
    general_trends_safety: str
    # Same-day escape hatch for a false positive: terms listed here are never
    # matched by the safety screen, so a bad word list can be defused without a
    # deploy. Lower-cased on the way in.
    general_trends_allow_terms: list[str]
    # Items whose source gives no publish time are KEPT by default: every source
    # populates it today, so a sudden None means the feed shape changed, and
    # dropping would turn a parser regression into a silent source outage.
    general_trends_drop_undated: bool
    # "" = the default Gemini model for the AI scoring pass.
    general_trends_model: str
    # "" = the fast Gemini model, for the cheap label + triage pass that runs
    # over raw items before clustering.
    general_trends_triage_model: str
    # LOOK IT UP BEFORE RECOMMENDING. "Make this" runs one Google-grounded
    # lookup of what the trend actually refers to, because a hashtag is a LABEL,
    # not a description: read literally, "#spidermanbrandnewday" produced three
    # ideas about "a fresh start" when it is the title of a Spider-Man film.
    # Off falls back to ideas written from the trend's stored summary alone.
    general_trends_research: bool
    # How long one lookup stays good. A trend's meaning does not change hourly,
    # and pressing "Make this" twice must not pay for a second search.
    general_trends_research_ttl_hours: int
    youtube_api_key: str
    # TikTok Creative Center hashtag regions (ISO-2). Must be codes the Creative
    # Center actually supports — an unsupported code silently returns US data.
    general_trends_tiktok_regions: list[str]
    # Instagram hashtag roster (no '#'). Empty keeps the hashtag lane off: it
    # needs `instagram_manage_insights` on the Graph token, and the API caps
    # UNIQUE hashtag names at 30 per rolling 7 days, so the roster is a weekly
    # decision, not a per-refresh one.
    general_trends_ig_hashtags: list[str]
    # Public IG Business/Creator handles (no '@') read via Business Discovery —
    # accounts that SET trends, not competitors. Empty keeps the lane off.
    general_trends_ig_watch_accounts: list[str]

    # --- Assets Library (v3 manual uploads) ---
    supabase_service_role_key: str
    producer_assets_bucket: str
    brand_docs_bucket: str
    # Bucket for generation media (Create Poster / Animation). Private by default
    # (served via signed URLs through the backend proxy). When
    # `producer_media_public` is on, it's a PUBLIC bucket and the Library serves
    # media straight from Supabase's CDN at permanent URLs (the fast path).
    producer_media_bucket: str
    # Opt-in: treat producer_media_bucket as PUBLIC. Gates BOTH bucket creation
    # publicness AND whether list responses emit public CDN / image-transform URLs
    # instead of the backend file proxy. Keep FALSE until the bucket is actually
    # public (a fresh project is created public; an EXISTING private bucket must be
    # flipped to public in the Supabase dashboard first), or media URLs will 400.
    producer_media_public: bool
    # Whether Supabase Image Transformation (/storage/v1/render/image/**) may be
    # used for thumbnails. It is a PAID add-on: on a project without it EVERY such
    # URL answers 403 {"code":"FeatureNotEnabled"} — and because those URLs go to
    # the BROWSER inside list JSON, the backend never sees the failure and every
    # tile silently renders "Preview unavailable" (#37).
    #   auto (default) — probe the endpoint once, cache the answer, re-check on a
    #                    TTL so re-enabling the add-on heals without a restart
    #   on / off       — force it, skipping the probe
    supabase_image_transform: str
    # Which object store asset_storage talks to. `auto` picks s3 when
    # S3_ENDPOINT is set, else supabase when SUPABASE_SERVICE_ROLE_KEY is set,
    # else local disk. The DB rows are the same in every mode, so switching
    # needs no schema change.
    storage_backend: str
    # S3-compatible store (MinIO in the compose stack). The endpoint is the
    # INTERNAL one the backend signs against (http://minio:9000); the public
    # base is where browsers and Instagram reach the same objects through Caddy
    # (https://<domain>/media). Both are stored without a trailing slash.
    s3_endpoint: str
    s3_public_base_url: str
    s3_access_key: str
    s3_secret_key: str
    s3_region: str
    producer_assets_root: Path
    # API key for `services/drive_folder.py` to enumerate a public Drive
    # folder's contents (so the user can bulk-import a folder of references).
    # Single-file Drive downloads don't need this — they work via the public
    # download endpoint. Folder listing requires the Drive v3 `files.list`
    # API which is keyed.
    google_drive_api_key: str

    # --- AI video editor media providers (optional) ---
    # services/media_providers.py reads these straight from os.getenv too;
    # mirrored here only for consistency/visibility. None = not configured.
    elevenlabs_api_key: str | None
    suno_api_key: str | None
    suno_api_base: str | None

    # --- Seedance 2.5 via Higgsfield (General AI Video, the Seedance engine) ---
    # Only the Seedance engine's "Generate video" needs these; the still and the
    # Veo engine run without them. None = not connected; the UI says so.
    higgsfield_api_key_id: str | None
    higgsfield_api_key_secret: str | None
    # Rolling 30-day cap on Seedance spend, summed from the yas_seedance_spend
    # ledger (one row per submission, refused jobs written off) so it survives
    # restarts and re-animated clips. 0 = no cap.
    seedance_budget_usd: float

    # --- Video caps ---
    max_video_duration_sec: int
    max_videos_per_run: int
    max_frames_per_video: int

    # --- CLIP ---
    use_clip: bool
    clip_model: str

    # --- Text chunking ---
    chunk_tokens: int
    chunk_overlap: int

    # --- FastAPI ---
    api_host: str
    api_port: int
    cors_origins: list[str]
    log_level: str

    # --- Scheduler ---
    auto_crawl_cron: str
    scheduler_timezone: str
    # Trend Radar: where its one JSON file lives, and the Gulf-time hour the
    # radar is built each morning (0-23; -1 turns the daily build off).
    trend_radar_root: Path
    trend_radar_hour: int

    # --- Producer Roadmap: scheduling, reminder email, IG publishing ---
    # Where the user opens 42 Studio (used in reminder-email links).
    app_base_url: str
    # Public base URL Instagram can fetch generated media from (e.g. a Cloudflare
    # tunnel or a deployed domain). Empty = live publishing disabled (self-gated).
    public_media_base_url: str
    # Recipient for scheduled-post reminder emails (single-tenant app).
    reminder_email: str
    # SMTP (reminder email). Host empty = email is a logged no-op (still queues).
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_use_tls: bool
    # Instagram content-publish token (needs instagram_content_publish scope on a
    # Business/Creator account). Empty = falls back to instagram_access_token;
    # publishing self-gates if neither works.
    instagram_publish_token: str
    # PUBLIC bucket (on the S3/MinIO store, or Supabase Storage) that hosts
    # calendar-booked media so Instagram can fetch it at publish time (replaces
    # PUBLIC_MEDIA_BASE_URL / tunnels). Only assets booked in the roadmap calendar
    # are uploaded here. Created on first use; anonymous read is GetObject only.
    approved_media_bucket: str
    # Arms the public-preview gate and signs the session cookie. Empty = the gate
    # is OFF (local dev). Set it before exposing the app publicly so visitors
    # can't burn OpenAI/Vertex spend without an account. Nobody types this value:
    # people sign in with an email + password from ACCOUNTS. See app/access.py.
    access_password: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            db_host=os.getenv("DB_HOST", "127.0.0.1"),
            db_port=_int("DB_PORT", 5432),
            db_name=os.getenv("DB_NAME", "postgres"),
            db_user=os.getenv("DB_USER", "postgres"),
            db_password=os.getenv("DB_PASSWORD", ""),
            db_sslmode=os.getenv("DB_SSLMODE", "require"),
            db_pool_min=_int("DB_POOL_MIN", 1),
            db_pool_max=_int("DB_POOL_MAX", 10),
            db_statement_timeout_ms=_int("DB_STATEMENT_TIMEOUT_MS", 15000),

            supabase_project_ref=os.getenv("SUPABASE_PROJECT_REF", ""),
            supabase_url=os.getenv("SUPABASE_URL", ""),
            supabase_access_token=os.getenv("SUPABASE_ACCESS_TOKEN", ""),

            target_url=os.getenv("TARGET_URL", "https://42abudhabi.ae"),
            target_domain=os.getenv("TARGET_DOMAIN", "42abudhabi.ae"),

            user_agent=os.getenv(
                "USER_AGENT",
                "ArchivistBot/0.2 (+contact: you@example.com)",
            ),
            request_timeout=_int("REQUEST_TIMEOUT", 20),
            run_concurrency_fetch=_int("RUN_CONCURRENCY_FETCH", 4),
            run_concurrency_openai=_int("RUN_CONCURRENCY_OPENAI", 2),

            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model_chat=os.getenv("OPENAI_MODEL_CHAT", "gpt-4o-mini"),
            openai_model_vision=os.getenv("OPENAI_MODEL_VISION", "gpt-4o-mini"),
            openai_model_synthesis=os.getenv("OPENAI_MODEL_SYNTHESIS", "gpt-4o"),
            openai_model_embed=os.getenv("OPENAI_MODEL_EMBED", "text-embedding-3-small"),
            openai_model_whisper=os.getenv("OPENAI_MODEL_WHISPER", "whisper-1"),
            openai_budget_usd=_float("OPENAI_BUDGET_USD", 10.0),
            llm_provider=(os.getenv("LLM_PROVIDER", "vertex").strip().lower() or "vertex"),

            asset_root=_path("ASSET_ROOT", "data/assets"),
            frames_root=_path("FRAMES_ROOT", "data/frames"),
            brand_root=_path("BRAND_ROOT", "data/brand"),
            upload_root=_path("UPLOAD_ROOT", "data/uploads"),
            producer_root=_path("PRODUCER_ROOT", "data/producer"),

            google_cloud_project=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
            google_cloud_location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
            google_genai_use_vertexai=_bool("GOOGLE_GENAI_USE_VERTEXAI", True),
            veo_output_bucket=os.getenv("VEO_OUTPUT_BUCKET", ""),
            veo_model=os.getenv("VEO_MODEL", "veo-3.1-fast-generate-001"),
            veo_model_premium=os.getenv("VEO_MODEL_PREMIUM", "veo-3.1-generate-001"),
            veo_budget_usd=_float("VEO_BUDGET_USD", 200.0),
            veo_max_duration_sec=_int("VEO_MAX_DURATION_SEC", 8),
            veo_default_aspect=os.getenv("VEO_DEFAULT_ASPECT", "16:9"),
            producer_max_script_cost_usd=_float("PRODUCER_MAX_SCRIPT_COST_USD", 3.00),
            vertex_gemini_model=os.getenv("VERTEX_GEMINI_MODEL", "gemini-2.5-pro"),
            vertex_gemini_fast_model=os.getenv("VERTEX_GEMINI_FAST_MODEL", "gemini-2.5-flash"),
            vertex_embed_model=os.getenv("VERTEX_EMBED_MODEL", "gemini-embedding-001"),
            vertex_embed_dim=_int("VERTEX_EMBED_DIM", 768),
            # "Nano Banana" — Gemini multimodal that outputs images. Override
            # to "imagen-3.0-generate-002" or similar if your project's Vertex
            # quota points at Imagen instead.
            vertex_image_model=os.getenv("VERTEX_IMAGE_MODEL", "gemini-2.5-flash-image"),
            # Nano Banana Pro. GA since 2026-05-28 under this id; the old
            # `gemini-3-pro-image-preview` id now 404s (and the 404 used to
            # drop every image call to the standard tier). Global endpoint only.
            vertex_image_pro_model=os.getenv(
                "VERTEX_IMAGE_PRO_MODEL", "gemini-3-pro-image"),
            vertex_image_pro_location=os.getenv("VERTEX_IMAGE_PRO_LOCATION", "global"),
            vertex_fallback_locations=_list("VERTEX_FALLBACK_LOCATIONS", "us-east4,us-west1,europe-west4"),
            vertex_max_concurrency=_int("VERTEX_MAX_CONCURRENCY", 4),
            claude_vertex_region=os.getenv("CLAUDE_VERTEX_REGION", "us-east5"),
            claude_model=os.getenv("CLAUDE_MODEL", "claude-opus-4-8"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-5"),
            ve_agent_provider=os.getenv("VE_AGENT_PROVIDER", "auto"),
            ve_agent_gemini_model=os.getenv("VE_AGENT_GEMINI_MODEL", "gemini-2.5-pro"),
            ve_agent_max_steps=_int("VE_AGENT_MAX_STEPS", 24),

            remotion_base_url=os.getenv("REMOTION_BASE_URL", "http://127.0.0.1:3001"),
            remotion_default_crossfade_sec=_float("REMOTION_DEFAULT_CROSSFADE_SEC", 0.5),

            instagram_access_token=os.getenv("INSTAGRAM_ACCESS_TOKEN", ""),
            instagram_account_id=os.getenv("INSTAGRAM_ACCOUNT_ID", ""),
            instagram_target_username=os.getenv("INSTAGRAM_TARGET_USERNAME", "42abudhabi"),
            instagram_api_version=os.getenv("INSTAGRAM_API_VERSION", "v21.0"),
            instagram_root=_path("INSTAGRAM_ROOT", "data/instagram"),
            instagram_max_videos_per_run=_int("INSTAGRAM_MAX_VIDEOS_PER_RUN", 100),

            # x_trends is DROPPED from the default list (the fetcher and parser
            # stay, so `GENERAL_TRENDS_SOURCES=...,x_trends` re-enables it). It
            # produced 80 cards and zero wins in the measured run: 240/240 of
            # its sightings had an empty summary and 0/240 carried a metric, so
            # every card was a bare hashtag the scoring model had to invent an
            # English meaning for ("Financial Transactions / Fintech", fit 75).
            # hacker_news / lemmy / github / google_news replace it — all
            # keyless, all carrying a real timestamp and (bar google_news) a
            # real metric. tiktok/instagram/instagram_watch are listed here but
            # self-skip until their blocker clears (Creative Center reachability,
            # a live Graph token).
            # Default FALSE — the stream is paused pending research, and a
            # default that silently restarted the collector on a fresh checkout
            # would defeat the pause. See the field's comment above.
            pollers_enabled=_bool("POLLERS_ENABLED", True),
            trend_auto_scan_hours=_int("TREND_AUTO_SCAN_HOURS", 24),
            general_trends_enabled=_bool("GENERAL_TRENDS_ENABLED", False),
            general_trends_sources=_list(
                "GENERAL_TRENDS_SOURCES",
                # The FORMAT REGISTRIES first — these are the only sources
                # allowed to create a card, so they are the feed. The rest
                # corroborate: they explain WHY a format exists (the
                # skyscraper-proposal case) without ever minting one.
                "tiktok,kym,kym_entries,kym_editorials,youtube_shorts,instagram,"
                "google_trends,bluesky,reddit,wikipedia,youtube,hacker_news,"
                "lemmy,github,google_news,instagram_watch",
            ),
            general_trends_geos=_list("GENERAL_TRENDS_GEOS", "US,AE"),
            # 7 subreddits -> 4. Dropped on measured evidence: r/videos scored
            # `avoid` on 19 of 24 sampled items, r/OutOfTheLoop was 68% junk AND
            # the source of the worst clustering over-fire ("What's going on
            # with X" titles collapsing into one mega-card), r/popular was pet
            # photos and fan art (13/24 at fit <= 10), and r/TikTokCringe is a
            # ragebait/thirst-trap host that Reddit's Atom gives us no NSFW flag
            # to catch. Fewer subs also shortens the Reddit lane from ~17 min to
            # ~4 (Reddit anon RSS allows about one request per minute per IP).
            general_trends_subreddits=_list(
                "GENERAL_TRENDS_SUBREDDITS",
                "memes,ProgrammerHumor,Damnthatsinteresting,technology",
            ),
            general_trends_refresh_hours=_int("GENERAL_TRENDS_REFRESH_HOURS", 6),
            general_trends_max_items_per_source=_int("GENERAL_TRENDS_MAX_ITEMS_PER_SOURCE", 40),
            # Reddit's old budget was 7 subs x 40 = 280 items while KYM — the
            # highest-precision source in the system, and the only one shipping
            # a name AND a written explanation — was structurally capped at 8.
            general_trends_source_caps=_int_map(
                # kym_entries is capped at its own per-run entry budget; the
                # Shorts chart is 50 rows and the default 40 would clip it.
                "GENERAL_TRENDS_SOURCE_CAPS",
                # youtube_shorts is capped WELL below the chart's 50 rows: at 50 x 3
                # regions it minted 103 of 167 cards on the first live run and the
                # feed became a song list. The chart is rank-ordered, so the top 15
                # keeps the most-used sounds and leaves room for the formats and
                # memes a social team can actually restage.
                "reddit:15,kym:40,kym_entries:40,kym_editorials:10,youtube_shorts:15",
            ),
            general_trends_feed_days=_int("GENERAL_TRENDS_FEED_DAYS", 7),
            general_trends_max_content_age_days=_int(
                "GENERAL_TRENDS_MAX_CONTENT_AGE_DAYS", 30,
            ),
            general_trends_kinds=_list(
                "GENERAL_TRENDS_KINDS", "meme,format,sound,challenge",
            ),
            # YouTube Charts' Shorts-usage chart is the only GEO-CORRECT
            # participatory source we found — AE and US genuinely return
            # different #1 tracks — which matters for a school in Abu Dhabi.
            # Using a sound IS recreating the format, so this is where most of
            # the feed's `sound` kind comes from.
            general_trends_shorts_regions=_list(
                "GENERAL_TRENDS_SHORTS_REGIONS", "AE,SA,US",
            ),
            # Fail closed: anything that is not one of the three known modes
            # falls back to "on", so a typo can never silently switch the
            # content screen off. (The tuple is inlined rather than imported
            # from general_trends_safety — services import config, not the
            # other way round.)
            general_trends_safety=_choice(
                "GENERAL_TRENDS_SAFETY", "on", ("on", "flag_only", "off"),
            ),
            general_trends_allow_terms=_lower_list("GENERAL_TRENDS_ALLOW_TERMS", ""),
            general_trends_drop_undated=_bool("GENERAL_TRENDS_DROP_UNDATED", False),
            general_trends_model=os.getenv("GENERAL_TRENDS_MODEL", "").strip(),
            general_trends_triage_model=os.getenv("GENERAL_TRENDS_TRIAGE_MODEL", "").strip(),
            # ON by default: the failure it prevents (ideas built on a misread
            # name) looks like a working feature, so it must not need enabling.
            general_trends_research=_bool("GENERAL_TRENDS_RESEARCH", True),
            general_trends_research_ttl_hours=_int(
                "GENERAL_TRENDS_RESEARCH_TTL_HOURS", 72,
            ),
            youtube_api_key=os.getenv("YOUTUBE_API_KEY", "").strip(),
            # Creative Center returns only THREE hashtags per (region, period)
            # before the login wall, so breadth of regions is the only lever on
            # how many format candidates a run sees at all: 4 regions was 24
            # candidates, 8 is ~48, for about 12 seconds of fetch.
            #
            # AE + SA + EG are the home market. PH, ID and MY are here because
            # of who actually lives in the UAE — Filipino and South-East Asian
            # communities are a large share of the audience, and their TikTok
            # formats land locally in a way US formats often do not. US + GB
            # are where a format usually starts before it reaches the Gulf.
            # (IN and PK would belong here too but Creative Center does not
            # offer them.) Duplicates across regions cluster into one card.
            general_trends_tiktok_regions=_list(
                "GENERAL_TRENDS_TIKTOK_REGIONS", "AE,SA,EG,PH,ID,MY,US,GB",
            ),
            general_trends_ig_hashtags=_list("GENERAL_TRENDS_IG_HASHTAGS", ""),
            general_trends_ig_watch_accounts=_list("GENERAL_TRENDS_IG_WATCH_ACCOUNTS", ""),

            supabase_service_role_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
            producer_assets_bucket=os.getenv("PRODUCER_ASSETS_BUCKET", "producer-assets"),
            brand_docs_bucket=os.getenv("BRAND_DOCS_BUCKET", "brand-docs"),
            producer_media_bucket=os.getenv("PRODUCER_MEDIA_BUCKET", "producer-media"),
            producer_media_public=_bool("PRODUCER_MEDIA_PUBLIC", False),
            supabase_image_transform=(
                os.getenv("SUPABASE_IMAGE_TRANSFORM", "auto").strip().lower() or "auto"
            ),
            storage_backend=_choice(
                "STORAGE_BACKEND", "auto", ("auto", "s3", "supabase", "local"),
            ),
            s3_endpoint=os.getenv("S3_ENDPOINT", "").strip().rstrip("/"),
            s3_public_base_url=os.getenv("S3_PUBLIC_BASE_URL", "").strip().rstrip("/"),
            s3_access_key=os.getenv("S3_ACCESS_KEY", "").strip(),
            s3_secret_key=os.getenv("S3_SECRET_KEY", "").strip(),
            s3_region=os.getenv("S3_REGION", "").strip() or "us-east-1",
            producer_assets_root=_path("PRODUCER_ASSETS_ROOT", "data/assets_library"),
            google_drive_api_key=os.getenv("GOOGLE_DRIVE_API_KEY", ""),

            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY") or None,
            suno_api_key=os.getenv("SUNO_API_KEY") or None,
            suno_api_base=os.getenv("SUNO_API_BASE") or None,

            higgsfield_api_key_id=os.getenv("HIGGSFIELD_API_KEY_ID") or None,
            higgsfield_api_key_secret=os.getenv("HIGGSFIELD_API_KEY_SECRET") or None,
            seedance_budget_usd=_float("SEEDANCE_BUDGET_USD", 50.0),

            max_video_duration_sec=_int("MAX_VIDEO_DURATION_SEC", 600),
            max_videos_per_run=_int("MAX_VIDEOS_PER_RUN", 5),
            max_frames_per_video=_int("MAX_FRAMES_PER_VIDEO", 30),

            use_clip=_bool("USE_CLIP", True),
            clip_model=os.getenv("CLIP_MODEL", "clip-ViT-B-32"),

            chunk_tokens=_int("CHUNK_TOKENS", 500),
            chunk_overlap=_int("CHUNK_OVERLAP", 50),

            api_host=os.getenv("API_HOST", "127.0.0.1"),
            api_port=_int("API_PORT", 8000),
            cors_origins=_list(
                "CORS_ORIGINS",
                "http://localhost:4242,http://127.0.0.1:4242",
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),

            auto_crawl_cron=os.getenv("AUTO_CRAWL_CRON", "").strip(),
            scheduler_timezone=os.getenv("SCHEDULER_TIMEZONE", "UTC").strip(),
            trend_radar_root=_path("TREND_RADAR_ROOT", "data/trend_radar"),
            trend_radar_hour=_int("TREND_RADAR_HOUR", 7),

            app_base_url=os.getenv("APP_BASE_URL", "http://127.0.0.1:4242").strip().rstrip("/"),
            public_media_base_url=os.getenv("PUBLIC_MEDIA_BASE_URL", "").strip().rstrip("/"),
            reminder_email=os.getenv("REMINDER_EMAIL", "").strip(),
            smtp_host=os.getenv("SMTP_HOST", "").strip(),
            smtp_port=_int("SMTP_PORT", 587),
            smtp_user=os.getenv("SMTP_USER", "").strip(),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_from=os.getenv("SMTP_FROM", "").strip(),
            smtp_use_tls=_bool("SMTP_USE_TLS", True),
            instagram_publish_token=os.getenv("INSTAGRAM_PUBLISH_TOKEN", "").strip(),
            approved_media_bucket=os.getenv("APPROVED_MEDIA_BUCKET", "approved-instagram").strip(),
            access_password=os.getenv("APP_ACCESS_PASSWORD", "").strip(),
        )


config = Config.from_env()
