-- 42 Studio schema for the self-hosted Postgres (pgvector/pgvector:pg17).
-- Generated from the live Supabase project with pg_dump 17 (--schema-only
-- --schema=public --no-owner --no-privileges), then adapted for plain Postgres:
--   * pgvector is installed into `public` here (Supabase keeps it in
--     `extensions`), so `extensions.vector` became `public.vector`.
--   * `CREATE SCHEMA public` dropped: a fresh database already has it.
-- Loaded once by the postgres container, on an empty data volume only.

BEGIN;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
--
-- PostgreSQL database dump
--


-- Dumped from database version 17.6
-- Dumped by pg_dump version 17.11 (Debian 17.11-1.pgdg12+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--



--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--



--
-- Name: instagram_posts_set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.instagram_posts_set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;


--
-- Name: producer_assets_set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.producer_assets_set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;


--
-- Name: search_hybrid(public.vector, integer, real, real, real); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.search_hybrid(query_embedding public.vector, k integer DEFAULT 10, w_text real DEFAULT 0.55, w_image real DEFAULT 0.25, w_video real DEFAULT 0.20) RETURNS TABLE(kind text, ref_id bigint, page_url text, snippet text, score real)
    LANGUAGE sql STABLE
    AS $$
    WITH
    t AS (
        SELECT 'text'::TEXT AS kind, tc.id AS ref_id, p.url AS page_url,
               LEFT(tc.chunk_text, 280) AS snippet,
               (w_text * (1 - (tc.embedding <=> query_embedding)))::REAL AS score
          FROM text_chunks tc
          JOIN website_pages p ON p.id = tc.page_id
         WHERE tc.embedding IS NOT NULL
         ORDER BY tc.embedding <=> query_embedding
         LIMIT k * 3
    ),
    i AS (
        SELECT 'image'::TEXT AS kind, a.id AS ref_id, p.url AS page_url,
               COALESCE(LEFT(a.ai_description, 280), a.source_url) AS snippet,
               (w_image * (1 - (a.description_embedding <=> query_embedding)))::REAL AS score
          FROM assets a
          JOIN website_pages p ON p.id = a.page_id
         WHERE a.description_embedding IS NOT NULL
         ORDER BY a.description_embedding <=> query_embedding
         LIMIT k * 3
    ),
    v AS (
        SELECT 'video'::TEXT AS kind, vt.id AS ref_id, p.url AS page_url,
               LEFT(vt.text, 280) AS snippet,
               (w_video * (1 - (vt.embedding <=> query_embedding)))::REAL AS score
          FROM video_transcripts vt
          JOIN assets a ON a.id = vt.asset_id
          JOIN website_pages p ON p.id = a.page_id
         WHERE vt.embedding IS NOT NULL
         ORDER BY vt.embedding <=> query_embedding
         LIMIT k * 3
    )
    SELECT * FROM (
        SELECT * FROM t UNION ALL
        SELECT * FROM i UNION ALL
        SELECT * FROM v
    ) u
    ORDER BY score DESC
    LIMIT k;
$$;


--
-- Name: search_text(public.vector, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.search_text(query_embedding public.vector, k integer DEFAULT 10) RETURNS TABLE(chunk_id bigint, page_id bigint, url text, page_title text, chunk_text text, similarity real)
    LANGUAGE sql STABLE
    AS $$
    SELECT tc.id, p.id, p.url, p.page_title, tc.chunk_text,
           (1 - (tc.embedding <=> query_embedding))::REAL
      FROM text_chunks tc
      JOIN website_pages p ON p.id = tc.page_id
     WHERE tc.embedding IS NOT NULL
     ORDER BY tc.embedding <=> query_embedding
     LIMIT k;
$$;


--
-- Name: search_visual(public.vector, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.search_visual(query_embedding public.vector, k integer DEFAULT 10) RETURNS TABLE(asset_id bigint, page_url text, ai_description text, similarity real)
    LANGUAGE sql STABLE
    AS $$
    SELECT a.id, p.url, a.ai_description,
           (1 - (ie.embedding <=> query_embedding))::REAL
      FROM image_embeddings ie
      JOIN assets a ON a.id = ie.asset_id
      JOIN website_pages p ON p.id = a.page_id
     ORDER BY ie.embedding <=> query_embedding
     LIMIT k;
$$;


--
-- Name: tg_producer_set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.tg_producer_set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END
$$;


--
-- Name: trg_set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trg_set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assets (
    id bigint NOT NULL,
    page_id bigint NOT NULL,
    asset_type text NOT NULL,
    source_url text NOT NULL,
    canonical_url text,
    url_hash text NOT NULL,
    perceptual_hash text,
    local_path text,
    width integer,
    height integer,
    bytes bigint,
    mime_type text,
    download_status text DEFAULT 'pending'::text NOT NULL,
    analyze_status text DEFAULT 'pending'::text NOT NULL,
    embed_status text DEFAULT 'pending'::text NOT NULL,
    ai_description text,
    description_embedding public.vector(1536),
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT assets_analyze_status_check CHECK ((analyze_status = ANY (ARRAY['pending'::text, 'completed'::text, 'failed'::text, 'skipped'::text]))),
    CONSTRAINT assets_asset_type_check CHECK ((asset_type = ANY (ARRAY['image'::text, 'video_native'::text, 'video_youtube'::text, 'video_vimeo'::text, 'instagram'::text, 'pdf'::text, 'iframe_other'::text]))),
    CONSTRAINT assets_download_status_check CHECK ((download_status = ANY (ARRAY['pending'::text, 'downloaded'::text, 'skipped'::text, 'failed'::text]))),
    CONSTRAINT assets_embed_status_check CHECK ((embed_status = ANY (ARRAY['pending'::text, 'completed'::text, 'failed'::text, 'skipped'::text])))
);


--
-- Name: assets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.assets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: assets_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.assets_id_seq OWNED BY public.assets.id;


--
-- Name: brand_documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.brand_documents (
    id bigint NOT NULL,
    doc_kind text NOT NULL,
    source_filename text,
    bytes bigint,
    mime_type text,
    storage_path text,
    raw_text text,
    ai_description text,
    ai_summary text,
    embedding public.vector(1536),
    visual_embedding public.vector(512),
    uploader_note text,
    upload_status text DEFAULT 'uploaded'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    storage_bucket text,
    CONSTRAINT brand_documents_doc_kind_check CHECK ((doc_kind = ANY (ARRAY['pdf'::text, 'docx'::text, 'text'::text, 'image'::text, 'free_text'::text, 'video'::text]))),
    CONSTRAINT brand_documents_upload_status_check CHECK ((upload_status = ANY (ARRAY['uploaded'::text, 'processing'::text, 'completed'::text, 'failed'::text])))
);


--
-- Name: brand_documents_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.brand_documents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: brand_documents_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.brand_documents_id_seq OWNED BY public.brand_documents.id;


--
-- Name: brand_profile_artifacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.brand_profile_artifacts (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT brand_profile_artifacts_kind_check CHECK ((kind = ANY (ARRAY['color_palette'::text, 'voice_tone'::text, 'topic_cluster'::text, 'visual_cluster'::text, 'final_markdown'::text, 'final_json'::text])))
);


--
-- Name: brand_profile_artifacts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.brand_profile_artifacts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: brand_profile_artifacts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.brand_profile_artifacts_id_seq OWNED BY public.brand_profile_artifacts.id;


--
-- Name: brand_profile_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.brand_profile_runs (
    id bigint NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    status text NOT NULL,
    notes text,
    CONSTRAINT brand_profile_runs_status_check CHECK ((status = ANY (ARRAY['started'::text, 'completed'::text, 'failed'::text])))
);


--
-- Name: brand_profile_latest; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.brand_profile_latest AS
 SELECT bpa.kind,
    bpa.payload,
    bpr.finished_at
   FROM (public.brand_profile_artifacts bpa
     JOIN public.brand_profile_runs bpr ON ((bpr.id = bpa.run_id)))
  WHERE (bpr.id = ( SELECT max(brand_profile_runs.id) AS max
           FROM public.brand_profile_runs
          WHERE (brand_profile_runs.status = 'completed'::text)));


--
-- Name: brand_profile_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.brand_profile_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: brand_profile_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.brand_profile_runs_id_seq OWNED BY public.brand_profile_runs.id;


--
-- Name: brand_profile_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.brand_profile_settings (
    id bigint NOT NULL,
    mode text DEFAULT 'auto'::text NOT NULL,
    weight_website integer DEFAULT 50 NOT NULL,
    weight_uploads integer DEFAULT 50 NOT NULL,
    weight_text integer DEFAULT 50 NOT NULL,
    weight_visual integer DEFAULT 50 NOT NULL,
    weight_audio integer DEFAULT 30 NOT NULL,
    weight_voice integer DEFAULT 33 NOT NULL,
    weight_visual_identity integer DEFAULT 33 NOT NULL,
    weight_themes integer DEFAULT 34 NOT NULL,
    model text DEFAULT 'gpt-4o'::text NOT NULL,
    temperature numeric(3,2) DEFAULT 0.30 NOT NULL,
    max_tokens integer DEFAULT 3000 NOT NULL,
    include_videos boolean DEFAULT true NOT NULL,
    include_images boolean DEFAULT true NOT NULL,
    include_uploads boolean DEFAULT true NOT NULL,
    include_website boolean DEFAULT true NOT NULL,
    custom_instructions text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT brand_profile_settings_max_tokens_check CHECK (((max_tokens >= 500) AND (max_tokens <= 16000))),
    CONSTRAINT brand_profile_settings_mode_check CHECK ((mode = ANY (ARRAY['auto'::text, 'manual'::text]))),
    CONSTRAINT brand_profile_settings_model_check CHECK ((model = ANY (ARRAY['gpt-4o'::text, 'gpt-4o-mini'::text, 'gpt-4-turbo'::text]))),
    CONSTRAINT brand_profile_settings_temperature_check CHECK (((temperature >= (0)::numeric) AND (temperature <= (2)::numeric))),
    CONSTRAINT brand_profile_settings_weight_audio_check CHECK (((weight_audio >= 0) AND (weight_audio <= 100))),
    CONSTRAINT brand_profile_settings_weight_text_check CHECK (((weight_text >= 0) AND (weight_text <= 100))),
    CONSTRAINT brand_profile_settings_weight_themes_check CHECK (((weight_themes >= 0) AND (weight_themes <= 100))),
    CONSTRAINT brand_profile_settings_weight_uploads_check CHECK (((weight_uploads >= 0) AND (weight_uploads <= 100))),
    CONSTRAINT brand_profile_settings_weight_visual_check CHECK (((weight_visual >= 0) AND (weight_visual <= 100))),
    CONSTRAINT brand_profile_settings_weight_visual_identity_check CHECK (((weight_visual_identity >= 0) AND (weight_visual_identity <= 100))),
    CONSTRAINT brand_profile_settings_weight_voice_check CHECK (((weight_voice >= 0) AND (weight_voice <= 100))),
    CONSTRAINT brand_profile_settings_weight_website_check CHECK (((weight_website >= 0) AND (weight_website <= 100)))
);


--
-- Name: brand_profile_settings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.brand_profile_settings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: brand_profile_settings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.brand_profile_settings_id_seq OWNED BY public.brand_profile_settings.id;


--
-- Name: edit_recipes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.edit_recipes (
    id bigint NOT NULL,
    source text NOT NULL,
    source_post_id bigint,
    source_generation_id bigint,
    recipe jsonb NOT NULL,
    embed_text text,
    embedding public.vector(768),
    engagement_score real DEFAULT 0,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: edit_recipes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.edit_recipes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: edit_recipes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.edit_recipes_id_seq OWNED BY public.edit_recipes.id;


--
-- Name: general_trend_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.general_trend_runs (
    id bigint NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    phase text,
    progress text,
    sources_total integer DEFAULT 0 NOT NULL,
    sources_done integer DEFAULT 0 NOT NULL,
    items_found integer DEFAULT 0 NOT NULL,
    trends_new integer DEFAULT 0 NOT NULL,
    trends_touched integer DEFAULT 0 NOT NULL,
    trends_scored integer DEFAULT 0 NOT NULL,
    error text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: general_trend_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.general_trend_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: general_trend_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.general_trend_runs_id_seq OWNED BY public.general_trend_runs.id;


--
-- Name: general_trend_sightings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.general_trend_sightings (
    id bigint NOT NULL,
    trend_id bigint NOT NULL,
    run_id bigint,
    source text NOT NULL,
    source_key text NOT NULL,
    title text,
    url text,
    image_url text,
    summary text,
    metric_value real,
    metric_label text,
    region text,
    category text,
    published_at timestamp with time zone,
    seen_at timestamp with time zone DEFAULT now() NOT NULL,
    extra jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: general_trend_sightings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.general_trend_sightings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: general_trend_sightings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.general_trend_sightings_id_seq OWNED BY public.general_trend_sightings.id;


--
-- Name: general_trends; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.general_trends (
    id bigint NOT NULL,
    slug text NOT NULL,
    title text NOT NULL,
    summary text,
    url text,
    image_url text,
    local_thumb_path text,
    category text,
    regions jsonb DEFAULT '[]'::jsonb NOT NULL,
    sources jsonb DEFAULT '[]'::jsonb NOT NULL,
    source_count integer DEFAULT 0 NOT NULL,
    sighting_count integer DEFAULT 0 NOT NULL,
    peak_metric real,
    velocity real,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    embedding public.vector(768),
    topic text,
    topic_summary text,
    theme text,
    trend_kind text,
    sensitivity text,
    fit_score integer,
    fit_reason text,
    uae_relevance integer,
    suggested_angle text,
    suggested_format text,
    effort text,
    tags jsonb DEFAULT '[]'::jsonb NOT NULL,
    opportunity_score real,
    scored_at timestamp with time zone,
    status text DEFAULT 'new'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    safety text DEFAULT 'ok'::text NOT NULL,
    safety_reason text,
    latest_content_at timestamp with time zone,
    evidence_count integer DEFAULT 0 NOT NULL,
    canonical_key text
);


--
-- Name: general_trends_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.general_trends_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: general_trends_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.general_trends_id_seq OWNED BY public.general_trends.id;


--
-- Name: image_embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.image_embeddings (
    asset_id bigint NOT NULL,
    model text NOT NULL,
    embedding public.vector(512) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: instagram_metrics_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.instagram_metrics_snapshots (
    id bigint NOT NULL,
    post_id bigint NOT NULL,
    captured_at timestamp with time zone DEFAULT now() NOT NULL,
    like_count integer,
    comments_count integer,
    view_count integer
);


--
-- Name: instagram_metrics_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.instagram_metrics_snapshots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: instagram_metrics_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.instagram_metrics_snapshots_id_seq OWNED BY public.instagram_metrics_snapshots.id;


--
-- Name: instagram_post_assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.instagram_post_assets (
    id bigint NOT NULL,
    post_id bigint NOT NULL,
    asset_id bigint NOT NULL,
    child_index integer DEFAULT 0 NOT NULL,
    ig_child_media_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: instagram_post_assets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.instagram_post_assets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: instagram_post_assets_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.instagram_post_assets_id_seq OWNED BY public.instagram_post_assets.id;


--
-- Name: instagram_posts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.instagram_posts (
    id bigint NOT NULL,
    ig_media_id text NOT NULL,
    account_username text NOT NULL,
    media_type text NOT NULL,
    media_product_type text,
    caption text,
    caption_embedding public.vector(768),
    permalink text,
    timestamp_posted timestamp with time zone,
    like_count integer,
    comments_count integer,
    view_count integer,
    children_count integer DEFAULT 0 NOT NULL,
    reach integer,
    impressions integer,
    plays integer,
    saved integer,
    ai_summary text,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_metrics_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT instagram_posts_media_type_check CHECK ((media_type = ANY (ARRAY['IMAGE'::text, 'VIDEO'::text, 'CAROUSEL_ALBUM'::text])))
);


--
-- Name: instagram_posts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.instagram_posts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: instagram_posts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.instagram_posts_id_seq OWNED BY public.instagram_posts.id;


--
-- Name: instagram_video_scenes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.instagram_video_scenes (
    id bigint NOT NULL,
    post_id bigint NOT NULL,
    asset_id bigint NOT NULL,
    scene_index integer NOT NULL,
    start_sec double precision NOT NULL,
    end_sec double precision NOT NULL,
    representative_frame_id bigint,
    description text NOT NULL,
    transcript_excerpt text,
    embedding public.vector(768),
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: instagram_video_scenes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.instagram_video_scenes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: instagram_video_scenes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.instagram_video_scenes_id_seq OWNED BY public.instagram_video_scenes.id;


--
-- Name: news_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_config (
    id integer DEFAULT 1 NOT NULL,
    brief text NOT NULL,
    stories_per_day integer DEFAULT 10 NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    next_run_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT news_config_singleton CHECK ((id = 1)),
    CONSTRAINT news_config_stories CHECK (((stories_per_day >= 3) AND (stories_per_day <= 20)))
);


--
-- Name: news_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.news_runs (
    id bigint NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    found integer DEFAULT 0 NOT NULL,
    kept integer DEFAULT 0 NOT NULL,
    drops jsonb DEFAULT '{}'::jsonb NOT NULL,
    error text,
    trigger text DEFAULT 'auto'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone
);


--
-- Name: news_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.news_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: news_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.news_runs_id_seq OWNED BY public.news_runs.id;


--
-- Name: page_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.page_links (
    id bigint NOT NULL,
    source_page_url text NOT NULL,
    target_page_url text NOT NULL,
    link_text text,
    link_type text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT page_links_link_type_check CHECK ((link_type = ANY (ARRAY['internal'::text, 'external'::text, 'asset'::text, 'unknown'::text])))
);


--
-- Name: page_links_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.page_links_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: page_links_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.page_links_id_seq OWNED BY public.page_links.id;


--
-- Name: producer_assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.producer_assets (
    id bigint NOT NULL,
    slug text NOT NULL,
    asset_kind text NOT NULL,
    category text,
    archetype text,
    display_name text,
    storage_bucket text,
    storage_path text,
    local_path text,
    mime_type text,
    bytes bigint,
    width integer,
    height integer,
    duration_sec double precision,
    source_post_id bigint,
    source_asset_id bigint,
    source_frame_id bigint,
    ai_description text DEFAULT ''::text NOT NULL,
    attributes jsonb DEFAULT '{}'::jsonb NOT NULL,
    embedding public.vector(768),
    usage_count integer DEFAULT 0 NOT NULL,
    last_used_at timestamp with time zone,
    enabled boolean DEFAULT true NOT NULL,
    uploaded_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT producer_assets_archetype_check CHECK (((archetype IS NULL) OR (archetype = ANY (ARRAY['emirati_male'::text, 'emirati_female'::text, 'casual_male'::text, 'casual_female'::text])))),
    CONSTRAINT producer_assets_asset_kind_check CHECK ((asset_kind = ANY (ARRAY['image'::text, 'video'::text, 'font'::text, 'audio'::text, 'other'::text])))
);


--
-- Name: producer_assets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.producer_assets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: producer_assets_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.producer_assets_id_seq OWNED BY public.producer_assets.id;


--
-- Name: producer_campaign_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.producer_campaign_items (
    id integer NOT NULL,
    campaign_id integer NOT NULL,
    cycle_index integer DEFAULT 0 NOT NULL,
    cycle_start date,
    slot_index integer DEFAULT 0 NOT NULL,
    kind text DEFAULT 'poster'::text NOT NULL,
    status text DEFAULT 'planned'::text NOT NULL,
    idea jsonb DEFAULT '{}'::jsonb NOT NULL,
    idea_title text,
    gen_kind text,
    gen_id bigint,
    caption text,
    proposed_time timestamp with time zone,
    scheduled_post_id bigint,
    attempt integer DEFAULT 0 NOT NULL,
    decline_note text,
    error text,
    cost_usd numeric DEFAULT 0 NOT NULL,
    notified_at timestamp with time zone,
    published_at timestamp with time zone,
    permalink text,
    claimed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: producer_campaign_items_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.producer_campaign_items_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: producer_campaign_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.producer_campaign_items_id_seq OWNED BY public.producer_campaign_items.id;


--
-- Name: producer_campaigns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.producer_campaigns (
    id integer NOT NULL,
    name text NOT NULL,
    topic_brief text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    status_reason text,
    posters_per_week integer DEFAULT 3 NOT NULL,
    videos_per_week integer DEFAULT 0 NOT NULL,
    duration_weeks integer,
    posting_windows jsonb DEFAULT '[]'::jsonb NOT NULL,
    poster_style text DEFAULT 'voxel'::text NOT NULL,
    video_duration_sec integer DEFAULT 6 NOT NULL,
    video_with_audio boolean DEFAULT true NOT NULL,
    weekly_budget_usd numeric,
    notify_email boolean DEFAULT true NOT NULL,
    cycle_index integer DEFAULT 0 NOT NULL,
    next_cycle_at timestamp with time zone,
    last_cycle_at timestamp with time zone,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: producer_campaigns_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.producer_campaigns_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: producer_campaigns_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.producer_campaigns_id_seq OWNED BY public.producer_campaigns.id;


--
-- Name: producer_compositions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.producer_compositions (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    template_id text NOT NULL,
    source_video_ids bigint[] NOT NULL,
    render_job_id text,
    output_path text,
    duration_sec numeric(8,3),
    cost_usd numeric(8,4) DEFAULT 0,
    status text DEFAULT 'queued'::text NOT NULL,
    decision_reason text,
    decision_at timestamp with time zone,
    error text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT producer_compositions_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'rendering'::text, 'ready'::text, 'failed'::text, 'approved'::text, 'declined'::text, 'retried'::text])))
);


--
-- Name: producer_compositions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.producer_compositions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: producer_compositions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.producer_compositions_id_seq OWNED BY public.producer_compositions.id;


--
-- Name: producer_feedback; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.producer_feedback (
    id bigint NOT NULL,
    target_kind text NOT NULL,
    target_id bigint NOT NULL,
    intent text NOT NULL,
    reason text,
    embedding public.vector(768),
    brand_profile_run_id bigint,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT producer_feedback_intent_check CHECK ((intent = ANY (ARRAY['approve'::text, 'decline'::text, 'retry'::text]))),
    CONSTRAINT producer_feedback_target_kind_check CHECK ((target_kind = ANY (ARRAY['idea'::text, 'script'::text, 'video'::text, 'reference'::text])))
);


--
-- Name: producer_feedback_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.producer_feedback_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: producer_feedback_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.producer_feedback_id_seq OWNED BY public.producer_feedback.id;


--
-- Name: producer_ideas; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.producer_ideas (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    idea_index integer NOT NULL,
    title text NOT NULL,
    concept text NOT NULL,
    visual_direction text NOT NULL,
    reason text NOT NULL,
    style_mood text NOT NULL,
    status text DEFAULT 'generated'::text NOT NULL,
    decision_reason text,
    decision_at timestamp with time zone,
    embedding public.vector(768),
    parent_idea_id bigint,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT producer_ideas_status_check CHECK ((status = ANY (ARRAY['generated'::text, 'approved'::text, 'declined'::text, 'retried'::text])))
);


--
-- Name: producer_ideas_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.producer_ideas_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: producer_ideas_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.producer_ideas_id_seq OWNED BY public.producer_ideas.id;


--
-- Name: producer_references; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.producer_references (
    id bigint NOT NULL,
    run_id bigint NOT NULL,
    kind text NOT NULL,
    original_path text,
    enhanced_path text,
    ai_description text,
    description_embedding public.vector(768),
    visual_embedding public.vector(512),
    raw_text text,
    status text DEFAULT 'uploaded'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT producer_references_kind_check CHECK ((kind = ANY (ARRAY['image'::text, 'video'::text, 'script_notes'::text, 'prompt'::text]))),
    CONSTRAINT producer_references_status_check CHECK ((status = ANY (ARRAY['uploaded'::text, 'described'::text, 'enhanced'::text, 'approved'::text, 'declined'::text])))
);


--
-- Name: producer_references_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.producer_references_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: producer_references_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.producer_references_id_seq OWNED BY public.producer_references.id;


--
-- Name: producer_roadmap; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.producer_roadmap (
    tool text NOT NULL,
    ref_id bigint NOT NULL,
    step text NOT NULL,
    state jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: producer_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.producer_runs (
    id bigint NOT NULL,
    mode text NOT NULL,
    status text DEFAULT 'started'::text NOT NULL,
    brand_profile_run_id bigint,
    title text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    notes text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT producer_runs_mode_check CHECK ((mode = ANY (ARRAY['auto'::text, 'user_idea'::text, 'reference'::text]))),
    CONSTRAINT producer_runs_status_check CHECK ((status = ANY (ARRAY['started'::text, 'ideas_pending'::text, 'idea_approved'::text, 'script_pending'::text, 'script_approved'::text, 'video_pending'::text, 'video_ready'::text, 'completed'::text, 'cancelled'::text, 'failed'::text])))
);


--
-- Name: producer_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.producer_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: producer_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.producer_runs_id_seq OWNED BY public.producer_runs.id;


--
-- Name: producer_scripts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.producer_scripts (
    id bigint NOT NULL,
    idea_id bigint NOT NULL,
    version_index integer NOT NULL,
    scenes jsonb NOT NULL,
    veo_prompt text NOT NULL,
    reference_image_paths text[],
    status text DEFAULT 'generated'::text NOT NULL,
    decision_reason text,
    decision_at timestamp with time zone,
    embedding public.vector(768),
    parent_script_id bigint,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT producer_scripts_status_check CHECK ((status = ANY (ARRAY['generated'::text, 'approved'::text, 'declined'::text, 'retried'::text])))
);


--
-- Name: producer_scripts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.producer_scripts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: producer_scripts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.producer_scripts_id_seq OWNED BY public.producer_scripts.id;


--
-- Name: producer_videos; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.producer_videos (
    id bigint NOT NULL,
    script_id bigint NOT NULL,
    scene_index integer,
    veo_model text NOT NULL,
    veo_operation_name text,
    gcs_uri text,
    local_path text,
    thumbnail_path text,
    duration_sec numeric(8,3),
    cost_usd numeric(8,4),
    status text DEFAULT 'generating'::text NOT NULL,
    decision_reason text,
    decision_at timestamp with time zone,
    parent_video_id bigint,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT producer_videos_status_check CHECK ((status = ANY (ARRAY['generating'::text, 'ready'::text, 'failed'::text, 'approved'::text, 'declined'::text, 'retried'::text])))
);


--
-- Name: producer_videos_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.producer_videos_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: producer_videos_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.producer_videos_id_seq OWNED BY public.producer_videos.id;


--
-- Name: scheduled_instagram_posts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scheduled_instagram_posts (
    id bigint NOT NULL,
    tool text NOT NULL,
    ref_id bigint NOT NULL,
    media_type text DEFAULT 'image'::text NOT NULL,
    caption text DEFAULT ''::text NOT NULL,
    scheduled_at timestamp with time zone NOT NULL,
    timezone text DEFAULT 'Asia/Dubai'::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    reminder_sent_at timestamp with time zone,
    published_at timestamp with time zone,
    ig_media_id text,
    permalink text,
    error text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    media_url text,
    reminder_count integer DEFAULT 0 NOT NULL
);


--
-- Name: scheduled_instagram_posts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.scheduled_instagram_posts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: scheduled_instagram_posts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.scheduled_instagram_posts_id_seq OWNED BY public.scheduled_instagram_posts.id;


--
-- Name: scrape_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scrape_runs (
    id bigint NOT NULL,
    target_domain text NOT NULL,
    sitemap_url text,
    status text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    pages_found_count integer DEFAULT 0 NOT NULL,
    pages_inserted_count integer DEFAULT 0 NOT NULL,
    errors_count integer DEFAULT 0 NOT NULL,
    notes text,
    CONSTRAINT scrape_runs_status_check CHECK ((status = ANY (ARRAY['started'::text, 'completed'::text, 'failed'::text])))
);


--
-- Name: scrape_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.scrape_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: scrape_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.scrape_runs_id_seq OWNED BY public.scrape_runs.id;


--
-- Name: text_chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.text_chunks (
    id bigint NOT NULL,
    page_id bigint NOT NULL,
    chunk_index integer NOT NULL,
    chunk_text text NOT NULL,
    token_count integer NOT NULL,
    embedding public.vector(1536),
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: text_chunks_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.text_chunks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: text_chunks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.text_chunks_id_seq OWNED BY public.text_chunks.id;


--
-- Name: trend_competitors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trend_competitors (
    id bigint NOT NULL,
    username text NOT NULL,
    display_name text,
    biography text,
    profile_picture_url text,
    followers_count bigint,
    media_count bigint,
    category text,
    region text DEFAULT 'uae'::text NOT NULL,
    why text,
    status text DEFAULT 'active'::text NOT NULL,
    source text DEFAULT 'manual'::text NOT NULL,
    median_engagement real,
    last_scanned_at timestamp with time zone,
    last_scan_status text,
    last_scan_error text,
    posts_found integer DEFAULT 0 NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: trend_competitors_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.trend_competitors_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: trend_competitors_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.trend_competitors_id_seq OWNED BY public.trend_competitors.id;


--
-- Name: trend_posts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trend_posts (
    id bigint NOT NULL,
    competitor_id bigint,
    ig_media_id text NOT NULL,
    username text,
    permalink text,
    media_type text,
    media_product_type text,
    media_url text,
    thumbnail_url text,
    local_thumb_path text,
    caption text,
    posted_at timestamp with time zone,
    like_count integer,
    comments_count integer,
    view_count bigint,
    engagement integer,
    engagement_rate real,
    outperformance real,
    topic text,
    topic_summary text,
    theme text,
    fit_score integer,
    fit_reason text,
    uae_relevance integer,
    suggested_angle text,
    suggested_format text,
    effort text,
    tags jsonb DEFAULT '[]'::jsonb NOT NULL,
    opportunity_score real,
    scored_at timestamp with time zone,
    status text DEFAULT 'new'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    source text DEFAULT 'competitor'::text NOT NULL,
    region text,
    language text,
    platform text
);


--
-- Name: trend_posts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.trend_posts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: trend_posts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.trend_posts_id_seq OWNED BY public.trend_posts.id;


--
-- Name: trend_scans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trend_scans (
    id bigint NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    phase text,
    progress text,
    competitors_total integer DEFAULT 0 NOT NULL,
    competitors_done integer DEFAULT 0 NOT NULL,
    posts_found integer DEFAULT 0 NOT NULL,
    posts_scored integer DEFAULT 0 NOT NULL,
    error text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: trend_scans_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.trend_scans_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: trend_scans_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.trend_scans_id_seq OWNED BY public.trend_scans.id;


--
-- Name: ve_voice_clone; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ve_voice_clone (
    id integer DEFAULT 1 NOT NULL,
    voice_id text,
    name text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: video_frames; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.video_frames (
    id bigint NOT NULL,
    asset_id bigint NOT NULL,
    timestamp_sec numeric(10,3) NOT NULL,
    frame_index integer NOT NULL,
    local_path text NOT NULL,
    ai_description text,
    embedding public.vector(512),
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: video_frames_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.video_frames_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: video_frames_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.video_frames_id_seq OWNED BY public.video_frames.id;


--
-- Name: video_summaries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.video_summaries (
    asset_id bigint NOT NULL,
    duration_sec numeric(10,3),
    frame_count integer,
    transcript_text text,
    summary text,
    embedding public.vector(1536),
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: video_transcripts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.video_transcripts (
    id bigint NOT NULL,
    asset_id bigint NOT NULL,
    segment_index integer NOT NULL,
    start_sec numeric(10,3) NOT NULL,
    end_sec numeric(10,3) NOT NULL,
    text text NOT NULL,
    embedding public.vector(1536)
);


--
-- Name: video_transcripts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.video_transcripts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: video_transcripts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.video_transcripts_id_seq OWNED BY public.video_transcripts.id;


--
-- Name: website_pages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.website_pages (
    id bigint NOT NULL,
    url text NOT NULL,
    normalized_url text,
    domain text,
    page_title text,
    source_type text,
    discovered_from text,
    sitemap_url text,
    http_status integer,
    content_type text,
    fetched_status text DEFAULT 'pending'::text NOT NULL,
    ai_analyzed_status text DEFAULT 'pending'::text NOT NULL,
    embedded_status text DEFAULT 'pending'::text NOT NULL,
    last_fetched_at timestamp with time zone,
    last_analyzed_at timestamp with time zone,
    last_embedded_at timestamp with time zone,
    raw_html text,
    extracted_text text,
    ai_summary text,
    embedding public.vector(1536),
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT website_pages_ai_analyzed_status_check CHECK ((ai_analyzed_status = ANY (ARRAY['pending'::text, 'completed'::text, 'failed'::text, 'skipped'::text]))),
    CONSTRAINT website_pages_embedded_status_check CHECK ((embedded_status = ANY (ARRAY['pending'::text, 'completed'::text, 'failed'::text, 'skipped'::text]))),
    CONSTRAINT website_pages_fetched_status_check CHECK ((fetched_status = ANY (ARRAY['pending'::text, 'fetched'::text, 'failed'::text, 'skipped'::text]))),
    CONSTRAINT website_pages_source_type_check CHECK ((source_type = ANY (ARRAY['sitemap'::text, 'internal_link'::text, 'manual'::text])))
);


--
-- Name: website_pages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.website_pages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: website_pages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.website_pages_id_seq OWNED BY public.website_pages.id;


--
-- Name: yas_animation_generations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.yas_animation_generations (
    id integer NOT NULL,
    prompt text,
    image_prompt_used text,
    motion_prompt_used text,
    status text DEFAULT 'frame_ready'::text NOT NULL,
    aspect_ratio text,
    style text,
    duration_sec integer,
    with_audio boolean,
    veo_model text,
    veo_operation_name text,
    gcs_uri text,
    frame_storage_bucket text,
    frame_storage_path text,
    frame_local_path text,
    video_storage_bucket text,
    video_storage_path text,
    video_local_path text,
    mime_type text,
    bytes integer,
    frame_cost_usd numeric,
    veo_cost_usd numeric,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    saved_asset_id bigint
);


--
-- Name: yas_animation_generations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.yas_animation_generations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: yas_animation_generations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.yas_animation_generations_id_seq OWNED BY public.yas_animation_generations.id;


--
-- Name: yas_animations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.yas_animations (
    id bigint NOT NULL,
    yas_generation_id bigint,
    prompt_used text NOT NULL,
    veo_model text DEFAULT 'veo-3.1-fast-generate-001'::text NOT NULL,
    veo_operation_name text,
    duration_sec integer DEFAULT 8 NOT NULL,
    with_audio boolean DEFAULT true NOT NULL,
    aspect_ratio text DEFAULT '1:1'::text NOT NULL,
    status text NOT NULL,
    gcs_uri text,
    storage_bucket text,
    storage_path text,
    local_path text,
    mime_type text,
    bytes integer,
    width integer,
    height integer,
    cost_usd numeric(10,4),
    error text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    gen_source text DEFAULT 'yas_generations'::text NOT NULL,
    poster_generation_id integer,
    CONSTRAINT yas_animations_status_check CHECK ((status = ANY (ARRAY['generating'::text, 'ready'::text, 'failed'::text])))
);


--
-- Name: yas_animations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.yas_animations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: yas_animations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.yas_animations_id_seq OWNED BY public.yas_animations.id;


--
-- Name: yas_general_video_generations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.yas_general_video_generations (
    id integer NOT NULL,
    prompt text,
    image_prompt_used text,
    motion_prompt_used text,
    status text DEFAULT 'frame_ready'::text NOT NULL,
    aspect_ratio text,
    duration_sec integer,
    with_audio boolean,
    reference_upload_ids text,
    veo_model text,
    veo_operation_name text,
    gcs_uri text,
    frame_storage_bucket text,
    frame_storage_path text,
    frame_local_path text,
    video_storage_bucket text,
    video_storage_path text,
    video_local_path text,
    mime_type text,
    bytes integer,
    frame_cost_usd numeric,
    veo_cost_usd numeric,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: yas_general_video_generations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.yas_general_video_generations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: yas_general_video_generations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.yas_general_video_generations_id_seq OWNED BY public.yas_general_video_generations.id;


--
-- Name: yas_generations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.yas_generations (
    id bigint NOT NULL,
    topic text NOT NULL,
    reference_asset_ids integer[] DEFAULT '{}'::integer[] NOT NULL,
    reference_upload_ids bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    parent_generation_id bigint,
    feedback_text text,
    retry_kind text,
    prompt_used text NOT NULL,
    storage_bucket text,
    storage_path text,
    local_path text,
    mime_type text,
    bytes integer,
    width integer,
    height integer,
    status text NOT NULL,
    saved_asset_id integer,
    cost_usd numeric(10,4),
    error text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    mode text DEFAULT 'style_inspired'::text NOT NULL,
    student_upload_id bigint,
    image_model text DEFAULT 'openai'::text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    script_embedding public.vector(768),
    variant_set_id bigint,
    CONSTRAINT yas_generations_image_model_check CHECK ((image_model = ANY (ARRAY['openai'::text, 'gemini_pro'::text]))),
    CONSTRAINT yas_generations_mode_check CHECK ((mode = ANY (ARRAY['style_inspired'::text, 'template_fill'::text, 'poster_human'::text, 'human_character'::text, 'talking_head_preview'::text, 'talking_head_intro'::text, 'talking_head_outro'::text, 'talking_head_scene'::text, 'talking_head_v2_preview'::text, 'talking_head_v2_intro'::text, 'talking_head_v2_outro'::text, 'talking_head_v2_scene'::text, 'video_edit_preview'::text]))),
    CONSTRAINT yas_generations_retry_kind_check CHECK (((retry_kind IS NULL) OR (retry_kind = ANY (ARRAY['tweak'::text, 'pivot'::text])))),
    CONSTRAINT yas_generations_status_check CHECK ((status = ANY (ARRAY['generating'::text, 'ready'::text, 'failed'::text, 'saved_to_assets'::text])))
);


--
-- Name: yas_generations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.yas_generations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: yas_generations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.yas_generations_id_seq OWNED BY public.yas_generations.id;


--
-- Name: yas_poster_generations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.yas_poster_generations (
    id integer NOT NULL,
    topic text,
    status text DEFAULT 'generating'::text NOT NULL,
    parent_generation_id integer,
    feedback_text text,
    retry_kind text,
    prompt_used text,
    mime_type text,
    bytes integer,
    width integer,
    height integer,
    cost_usd numeric,
    error text,
    mode text,
    reference_asset_ids integer[] DEFAULT '{}'::integer[] NOT NULL,
    reference_upload_ids integer[] DEFAULT '{}'::integer[] NOT NULL,
    saved_asset_id integer,
    variant_set_id integer,
    concept text,
    variation_of integer,
    image_usage text,
    image_model text,
    style text,
    storage_bucket text,
    storage_path text,
    local_path text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: yas_poster_generations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.yas_poster_generations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: yas_poster_generations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.yas_poster_generations_id_seq OWNED BY public.yas_poster_generations.id;


--
-- Name: yas_uploads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.yas_uploads (
    id bigint NOT NULL,
    token text NOT NULL,
    local_path text NOT NULL,
    mime_type text NOT NULL,
    bytes integer NOT NULL,
    width integer,
    height integer,
    ai_description text,
    created_at timestamp with time zone DEFAULT now(),
    duration_sec numeric,
    thumb_path text
);


--
-- Name: yas_uploads_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.yas_uploads_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: yas_uploads_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.yas_uploads_id_seq OWNED BY public.yas_uploads.id;


--
-- Name: yas_video_edit_revisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.yas_video_edit_revisions (
    id bigint NOT NULL,
    project_id bigint NOT NULL,
    rev integer NOT NULL,
    actor text,
    summary text,
    doc_hash text NOT NULL,
    doc jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: yas_video_edit_revisions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.yas_video_edit_revisions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: yas_video_edit_revisions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.yas_video_edit_revisions_id_seq OWNED BY public.yas_video_edit_revisions.id;


--
-- Name: assets id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets ALTER COLUMN id SET DEFAULT nextval('public.assets_id_seq'::regclass);


--
-- Name: brand_documents id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_documents ALTER COLUMN id SET DEFAULT nextval('public.brand_documents_id_seq'::regclass);


--
-- Name: brand_profile_artifacts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_profile_artifacts ALTER COLUMN id SET DEFAULT nextval('public.brand_profile_artifacts_id_seq'::regclass);


--
-- Name: brand_profile_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_profile_runs ALTER COLUMN id SET DEFAULT nextval('public.brand_profile_runs_id_seq'::regclass);


--
-- Name: brand_profile_settings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_profile_settings ALTER COLUMN id SET DEFAULT nextval('public.brand_profile_settings_id_seq'::regclass);


--
-- Name: edit_recipes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edit_recipes ALTER COLUMN id SET DEFAULT nextval('public.edit_recipes_id_seq'::regclass);


--
-- Name: general_trend_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.general_trend_runs ALTER COLUMN id SET DEFAULT nextval('public.general_trend_runs_id_seq'::regclass);


--
-- Name: general_trend_sightings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.general_trend_sightings ALTER COLUMN id SET DEFAULT nextval('public.general_trend_sightings_id_seq'::regclass);


--
-- Name: general_trends id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.general_trends ALTER COLUMN id SET DEFAULT nextval('public.general_trends_id_seq'::regclass);


--
-- Name: instagram_metrics_snapshots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_metrics_snapshots ALTER COLUMN id SET DEFAULT nextval('public.instagram_metrics_snapshots_id_seq'::regclass);


--
-- Name: instagram_post_assets id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_post_assets ALTER COLUMN id SET DEFAULT nextval('public.instagram_post_assets_id_seq'::regclass);


--
-- Name: instagram_posts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_posts ALTER COLUMN id SET DEFAULT nextval('public.instagram_posts_id_seq'::regclass);


--
-- Name: instagram_video_scenes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_video_scenes ALTER COLUMN id SET DEFAULT nextval('public.instagram_video_scenes_id_seq'::regclass);


--
-- Name: news_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_runs ALTER COLUMN id SET DEFAULT nextval('public.news_runs_id_seq'::regclass);


--
-- Name: page_links id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.page_links ALTER COLUMN id SET DEFAULT nextval('public.page_links_id_seq'::regclass);


--
-- Name: producer_assets id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_assets ALTER COLUMN id SET DEFAULT nextval('public.producer_assets_id_seq'::regclass);


--
-- Name: producer_campaign_items id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_campaign_items ALTER COLUMN id SET DEFAULT nextval('public.producer_campaign_items_id_seq'::regclass);


--
-- Name: producer_campaigns id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_campaigns ALTER COLUMN id SET DEFAULT nextval('public.producer_campaigns_id_seq'::regclass);


--
-- Name: producer_compositions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_compositions ALTER COLUMN id SET DEFAULT nextval('public.producer_compositions_id_seq'::regclass);


--
-- Name: producer_feedback id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_feedback ALTER COLUMN id SET DEFAULT nextval('public.producer_feedback_id_seq'::regclass);


--
-- Name: producer_ideas id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_ideas ALTER COLUMN id SET DEFAULT nextval('public.producer_ideas_id_seq'::regclass);


--
-- Name: producer_references id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_references ALTER COLUMN id SET DEFAULT nextval('public.producer_references_id_seq'::regclass);


--
-- Name: producer_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_runs ALTER COLUMN id SET DEFAULT nextval('public.producer_runs_id_seq'::regclass);


--
-- Name: producer_scripts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_scripts ALTER COLUMN id SET DEFAULT nextval('public.producer_scripts_id_seq'::regclass);


--
-- Name: producer_videos id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_videos ALTER COLUMN id SET DEFAULT nextval('public.producer_videos_id_seq'::regclass);


--
-- Name: scheduled_instagram_posts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_instagram_posts ALTER COLUMN id SET DEFAULT nextval('public.scheduled_instagram_posts_id_seq'::regclass);


--
-- Name: scrape_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scrape_runs ALTER COLUMN id SET DEFAULT nextval('public.scrape_runs_id_seq'::regclass);


--
-- Name: text_chunks id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.text_chunks ALTER COLUMN id SET DEFAULT nextval('public.text_chunks_id_seq'::regclass);


--
-- Name: trend_competitors id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trend_competitors ALTER COLUMN id SET DEFAULT nextval('public.trend_competitors_id_seq'::regclass);


--
-- Name: trend_posts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trend_posts ALTER COLUMN id SET DEFAULT nextval('public.trend_posts_id_seq'::regclass);


--
-- Name: trend_scans id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trend_scans ALTER COLUMN id SET DEFAULT nextval('public.trend_scans_id_seq'::regclass);


--
-- Name: video_frames id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_frames ALTER COLUMN id SET DEFAULT nextval('public.video_frames_id_seq'::regclass);


--
-- Name: video_transcripts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_transcripts ALTER COLUMN id SET DEFAULT nextval('public.video_transcripts_id_seq'::regclass);


--
-- Name: website_pages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.website_pages ALTER COLUMN id SET DEFAULT nextval('public.website_pages_id_seq'::regclass);


--
-- Name: yas_animation_generations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_animation_generations ALTER COLUMN id SET DEFAULT nextval('public.yas_animation_generations_id_seq'::regclass);


--
-- Name: yas_animations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_animations ALTER COLUMN id SET DEFAULT nextval('public.yas_animations_id_seq'::regclass);


--
-- Name: yas_general_video_generations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_general_video_generations ALTER COLUMN id SET DEFAULT nextval('public.yas_general_video_generations_id_seq'::regclass);


--
-- Name: yas_generations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_generations ALTER COLUMN id SET DEFAULT nextval('public.yas_generations_id_seq'::regclass);


--
-- Name: yas_poster_generations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_poster_generations ALTER COLUMN id SET DEFAULT nextval('public.yas_poster_generations_id_seq'::regclass);


--
-- Name: yas_uploads id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_uploads ALTER COLUMN id SET DEFAULT nextval('public.yas_uploads_id_seq'::regclass);


--
-- Name: yas_video_edit_revisions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_video_edit_revisions ALTER COLUMN id SET DEFAULT nextval('public.yas_video_edit_revisions_id_seq'::regclass);


--
-- Name: assets assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_pkey PRIMARY KEY (id);


--
-- Name: assets assets_url_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_url_hash_key UNIQUE (url_hash);


--
-- Name: brand_documents brand_documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_documents
    ADD CONSTRAINT brand_documents_pkey PRIMARY KEY (id);


--
-- Name: brand_profile_artifacts brand_profile_artifacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_profile_artifacts
    ADD CONSTRAINT brand_profile_artifacts_pkey PRIMARY KEY (id);


--
-- Name: brand_profile_runs brand_profile_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_profile_runs
    ADD CONSTRAINT brand_profile_runs_pkey PRIMARY KEY (id);


--
-- Name: brand_profile_settings brand_profile_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_profile_settings
    ADD CONSTRAINT brand_profile_settings_pkey PRIMARY KEY (id);


--
-- Name: edit_recipes edit_recipes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edit_recipes
    ADD CONSTRAINT edit_recipes_pkey PRIMARY KEY (id);


--
-- Name: general_trend_runs general_trend_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.general_trend_runs
    ADD CONSTRAINT general_trend_runs_pkey PRIMARY KEY (id);


--
-- Name: general_trend_sightings general_trend_sightings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.general_trend_sightings
    ADD CONSTRAINT general_trend_sightings_pkey PRIMARY KEY (id);


--
-- Name: general_trend_sightings general_trend_sightings_run_id_source_source_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.general_trend_sightings
    ADD CONSTRAINT general_trend_sightings_run_id_source_source_key_key UNIQUE (run_id, source, source_key);


--
-- Name: general_trends general_trends_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.general_trends
    ADD CONSTRAINT general_trends_pkey PRIMARY KEY (id);


--
-- Name: general_trends general_trends_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.general_trends
    ADD CONSTRAINT general_trends_slug_key UNIQUE (slug);


--
-- Name: image_embeddings image_embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.image_embeddings
    ADD CONSTRAINT image_embeddings_pkey PRIMARY KEY (asset_id);


--
-- Name: instagram_metrics_snapshots instagram_metrics_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_metrics_snapshots
    ADD CONSTRAINT instagram_metrics_snapshots_pkey PRIMARY KEY (id);


--
-- Name: instagram_post_assets instagram_post_assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_post_assets
    ADD CONSTRAINT instagram_post_assets_pkey PRIMARY KEY (id);


--
-- Name: instagram_post_assets instagram_post_assets_post_id_child_index_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_post_assets
    ADD CONSTRAINT instagram_post_assets_post_id_child_index_key UNIQUE (post_id, child_index);


--
-- Name: instagram_posts instagram_posts_ig_media_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_posts
    ADD CONSTRAINT instagram_posts_ig_media_id_key UNIQUE (ig_media_id);


--
-- Name: instagram_posts instagram_posts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_posts
    ADD CONSTRAINT instagram_posts_pkey PRIMARY KEY (id);


--
-- Name: instagram_video_scenes instagram_video_scenes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_video_scenes
    ADD CONSTRAINT instagram_video_scenes_pkey PRIMARY KEY (id);


--
-- Name: instagram_video_scenes instagram_video_scenes_post_id_scene_index_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_video_scenes
    ADD CONSTRAINT instagram_video_scenes_post_id_scene_index_key UNIQUE (post_id, scene_index);


--
-- Name: news_config news_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_config
    ADD CONSTRAINT news_config_pkey PRIMARY KEY (id);


--
-- Name: news_runs news_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_runs
    ADD CONSTRAINT news_runs_pkey PRIMARY KEY (id);


--
-- Name: page_links page_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.page_links
    ADD CONSTRAINT page_links_pkey PRIMARY KEY (id);


--
-- Name: page_links page_links_source_page_url_target_page_url_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.page_links
    ADD CONSTRAINT page_links_source_page_url_target_page_url_key UNIQUE (source_page_url, target_page_url);


--
-- Name: producer_assets producer_assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_assets
    ADD CONSTRAINT producer_assets_pkey PRIMARY KEY (id);


--
-- Name: producer_assets producer_assets_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_assets
    ADD CONSTRAINT producer_assets_slug_key UNIQUE (slug);


--
-- Name: producer_campaign_items producer_campaign_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_campaign_items
    ADD CONSTRAINT producer_campaign_items_pkey PRIMARY KEY (id);


--
-- Name: producer_campaigns producer_campaigns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_campaigns
    ADD CONSTRAINT producer_campaigns_pkey PRIMARY KEY (id);


--
-- Name: producer_compositions producer_compositions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_compositions
    ADD CONSTRAINT producer_compositions_pkey PRIMARY KEY (id);


--
-- Name: producer_feedback producer_feedback_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_feedback
    ADD CONSTRAINT producer_feedback_pkey PRIMARY KEY (id);


--
-- Name: producer_ideas producer_ideas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_ideas
    ADD CONSTRAINT producer_ideas_pkey PRIMARY KEY (id);


--
-- Name: producer_references producer_references_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_references
    ADD CONSTRAINT producer_references_pkey PRIMARY KEY (id);


--
-- Name: producer_roadmap producer_roadmap_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_roadmap
    ADD CONSTRAINT producer_roadmap_pkey PRIMARY KEY (tool, ref_id);


--
-- Name: producer_runs producer_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_runs
    ADD CONSTRAINT producer_runs_pkey PRIMARY KEY (id);


--
-- Name: producer_scripts producer_scripts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_scripts
    ADD CONSTRAINT producer_scripts_pkey PRIMARY KEY (id);


--
-- Name: producer_videos producer_videos_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_videos
    ADD CONSTRAINT producer_videos_pkey PRIMARY KEY (id);


--
-- Name: scheduled_instagram_posts scheduled_instagram_posts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_instagram_posts
    ADD CONSTRAINT scheduled_instagram_posts_pkey PRIMARY KEY (id);


--
-- Name: scrape_runs scrape_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scrape_runs
    ADD CONSTRAINT scrape_runs_pkey PRIMARY KEY (id);


--
-- Name: text_chunks text_chunks_page_id_chunk_index_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.text_chunks
    ADD CONSTRAINT text_chunks_page_id_chunk_index_key UNIQUE (page_id, chunk_index);


--
-- Name: text_chunks text_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.text_chunks
    ADD CONSTRAINT text_chunks_pkey PRIMARY KEY (id);


--
-- Name: trend_competitors trend_competitors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trend_competitors
    ADD CONSTRAINT trend_competitors_pkey PRIMARY KEY (id);


--
-- Name: trend_competitors trend_competitors_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trend_competitors
    ADD CONSTRAINT trend_competitors_username_key UNIQUE (username);


--
-- Name: trend_posts trend_posts_ig_media_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trend_posts
    ADD CONSTRAINT trend_posts_ig_media_id_key UNIQUE (ig_media_id);


--
-- Name: trend_posts trend_posts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trend_posts
    ADD CONSTRAINT trend_posts_pkey PRIMARY KEY (id);


--
-- Name: trend_scans trend_scans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trend_scans
    ADD CONSTRAINT trend_scans_pkey PRIMARY KEY (id);


--
-- Name: ve_voice_clone ve_voice_clone_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ve_voice_clone
    ADD CONSTRAINT ve_voice_clone_pkey PRIMARY KEY (id);


--
-- Name: video_frames video_frames_asset_id_frame_index_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_frames
    ADD CONSTRAINT video_frames_asset_id_frame_index_key UNIQUE (asset_id, frame_index);


--
-- Name: video_frames video_frames_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_frames
    ADD CONSTRAINT video_frames_pkey PRIMARY KEY (id);


--
-- Name: video_summaries video_summaries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_summaries
    ADD CONSTRAINT video_summaries_pkey PRIMARY KEY (asset_id);


--
-- Name: video_transcripts video_transcripts_asset_id_segment_index_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_transcripts
    ADD CONSTRAINT video_transcripts_asset_id_segment_index_key UNIQUE (asset_id, segment_index);


--
-- Name: video_transcripts video_transcripts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_transcripts
    ADD CONSTRAINT video_transcripts_pkey PRIMARY KEY (id);


--
-- Name: website_pages website_pages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.website_pages
    ADD CONSTRAINT website_pages_pkey PRIMARY KEY (id);


--
-- Name: website_pages website_pages_url_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.website_pages
    ADD CONSTRAINT website_pages_url_key UNIQUE (url);


--
-- Name: yas_animation_generations yas_animation_generations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_animation_generations
    ADD CONSTRAINT yas_animation_generations_pkey PRIMARY KEY (id);


--
-- Name: yas_animations yas_animations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_animations
    ADD CONSTRAINT yas_animations_pkey PRIMARY KEY (id);


--
-- Name: yas_general_video_generations yas_general_video_generations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_general_video_generations
    ADD CONSTRAINT yas_general_video_generations_pkey PRIMARY KEY (id);


--
-- Name: yas_generations yas_generations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_generations
    ADD CONSTRAINT yas_generations_pkey PRIMARY KEY (id);


--
-- Name: yas_poster_generations yas_poster_generations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_poster_generations
    ADD CONSTRAINT yas_poster_generations_pkey PRIMARY KEY (id);


--
-- Name: yas_uploads yas_uploads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_uploads
    ADD CONSTRAINT yas_uploads_pkey PRIMARY KEY (id);


--
-- Name: yas_uploads yas_uploads_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_uploads
    ADD CONSTRAINT yas_uploads_token_key UNIQUE (token);


--
-- Name: yas_video_edit_revisions yas_video_edit_revisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_video_edit_revisions
    ADD CONSTRAINT yas_video_edit_revisions_pkey PRIMARY KEY (id);


--
-- Name: yas_video_edit_revisions yas_video_edit_revisions_project_id_rev_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_video_edit_revisions
    ADD CONSTRAINT yas_video_edit_revisions_project_id_rev_key UNIQUE (project_id, rev);


--
-- Name: edit_recipes_source_generation_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX edit_recipes_source_generation_uq ON public.edit_recipes USING btree (source, source_generation_id) WHERE (source_generation_id IS NOT NULL);


--
-- Name: edit_recipes_source_post_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX edit_recipes_source_post_uq ON public.edit_recipes USING btree (source, source_post_id) WHERE (source_post_id IS NOT NULL);


--
-- Name: idx_assets_analyze; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_analyze ON public.assets USING btree (analyze_status);


--
-- Name: idx_assets_desc_vec; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_desc_vec ON public.assets USING hnsw (description_embedding public.vector_cosine_ops) WHERE (description_embedding IS NOT NULL);


--
-- Name: idx_assets_embed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_embed ON public.assets USING btree (embed_status);


--
-- Name: idx_assets_page; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_page ON public.assets USING btree (page_id);


--
-- Name: idx_assets_phash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_phash ON public.assets USING btree (perceptual_hash) WHERE (perceptual_hash IS NOT NULL);


--
-- Name: idx_assets_type_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_type_status ON public.assets USING btree (asset_type, download_status);


--
-- Name: idx_bpa_run_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bpa_run_kind ON public.brand_profile_artifacts USING btree (run_id, kind);


--
-- Name: idx_brand_docs_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_brand_docs_kind ON public.brand_documents USING btree (doc_kind);


--
-- Name: idx_brand_docs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_brand_docs_status ON public.brand_documents USING btree (upload_status);


--
-- Name: idx_brand_docs_text_vec; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_brand_docs_text_vec ON public.brand_documents USING hnsw (embedding public.vector_cosine_ops) WHERE (embedding IS NOT NULL);


--
-- Name: idx_brand_docs_visual_vec; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_brand_docs_visual_vec ON public.brand_documents USING hnsw (visual_embedding public.vector_cosine_ops) WHERE (visual_embedding IS NOT NULL);


--
-- Name: idx_campaign_item_slot; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_campaign_item_slot ON public.producer_campaign_items USING btree (campaign_id, cycle_index, slot_index);


--
-- Name: idx_campaign_item_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_campaign_item_status ON public.producer_campaign_items USING btree (status, campaign_id);


--
-- Name: idx_general_trend_runs_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_general_trend_runs_recent ON public.general_trend_runs USING btree (started_at DESC);


--
-- Name: idx_general_trend_sightings_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_general_trend_sightings_key ON public.general_trend_sightings USING btree (source, source_key, seen_at DESC);


--
-- Name: idx_general_trend_sightings_trend; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_general_trend_sightings_trend ON public.general_trend_sightings USING btree (trend_id, seen_at DESC);


--
-- Name: idx_general_trends_canonical; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_general_trends_canonical ON public.general_trends USING btree (canonical_key) WHERE (canonical_key IS NOT NULL);


--
-- Name: idx_general_trends_feed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_general_trends_feed ON public.general_trends USING btree (last_seen_at DESC) WHERE (safety = 'ok'::text);


--
-- Name: idx_general_trends_rank; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_general_trends_rank ON public.general_trends USING btree (status, opportunity_score DESC NULLS LAST);


--
-- Name: idx_general_trends_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_general_trends_seen ON public.general_trends USING btree (last_seen_at DESC);


--
-- Name: idx_general_trends_unscored; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_general_trends_unscored ON public.general_trends USING btree (scored_at) WHERE (scored_at IS NULL);


--
-- Name: idx_image_embeddings_vec; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_image_embeddings_vec ON public.image_embeddings USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: idx_news_runs_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_news_runs_recent ON public.news_runs USING btree (started_at DESC);


--
-- Name: idx_page_links_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_page_links_source ON public.page_links USING btree (source_page_url);


--
-- Name: idx_page_links_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_page_links_target ON public.page_links USING btree (target_page_url);


--
-- Name: idx_pages_summary_vec; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pages_summary_vec ON public.website_pages USING hnsw (embedding public.vector_cosine_ops) WHERE (embedding IS NOT NULL);


--
-- Name: idx_producer_compositions_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_producer_compositions_job ON public.producer_compositions USING btree (render_job_id) WHERE (status = ANY (ARRAY['queued'::text, 'rendering'::text]));


--
-- Name: idx_producer_compositions_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_producer_compositions_run ON public.producer_compositions USING btree (run_id);


--
-- Name: idx_producer_feedback_tgt; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_producer_feedback_tgt ON public.producer_feedback USING btree (target_kind, target_id);


--
-- Name: idx_producer_feedback_vec; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_producer_feedback_vec ON public.producer_feedback USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: idx_producer_ideas_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_producer_ideas_run ON public.producer_ideas USING btree (run_id);


--
-- Name: idx_producer_ideas_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_producer_ideas_status ON public.producer_ideas USING btree (status);


--
-- Name: idx_producer_ideas_vec; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_producer_ideas_vec ON public.producer_ideas USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: idx_producer_refs_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_producer_refs_run ON public.producer_references USING btree (run_id);


--
-- Name: idx_producer_runs_started; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_producer_runs_started ON public.producer_runs USING btree (started_at DESC);


--
-- Name: idx_producer_runs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_producer_runs_status ON public.producer_runs USING btree (status);


--
-- Name: idx_producer_scripts_embedding_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_producer_scripts_embedding_hnsw ON public.producer_scripts USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: idx_producer_scripts_idea; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_producer_scripts_idea ON public.producer_scripts USING btree (idea_id);


--
-- Name: idx_producer_videos_op; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_producer_videos_op ON public.producer_videos USING btree (veo_operation_name) WHERE (status = 'generating'::text);


--
-- Name: idx_producer_videos_script; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_producer_videos_script ON public.producer_videos USING btree (script_id);


--
-- Name: idx_sched_ig_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sched_ig_due ON public.scheduled_instagram_posts USING btree (status, scheduled_at);


--
-- Name: idx_scrape_runs_started_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scrape_runs_started_at ON public.scrape_runs USING btree (started_at DESC);


--
-- Name: idx_text_chunks_page; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_text_chunks_page ON public.text_chunks USING btree (page_id);


--
-- Name: idx_text_chunks_vec; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_text_chunks_vec ON public.text_chunks USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: idx_trend_competitors_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trend_competitors_status ON public.trend_competitors USING btree (status, region);


--
-- Name: idx_trend_posts_competitor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trend_posts_competitor ON public.trend_posts USING btree (competitor_id);


--
-- Name: idx_trend_posts_rank; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trend_posts_rank ON public.trend_posts USING btree (status, opportunity_score DESC NULLS LAST);


--
-- Name: idx_trend_posts_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trend_posts_source ON public.trend_posts USING btree (source);


--
-- Name: idx_trend_posts_theme; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trend_posts_theme ON public.trend_posts USING btree (theme);


--
-- Name: idx_trend_posts_unscored; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trend_posts_unscored ON public.trend_posts USING btree (scored_at) WHERE (scored_at IS NULL);


--
-- Name: idx_trend_scans_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_trend_scans_recent ON public.trend_scans USING btree (started_at DESC);


--
-- Name: idx_video_frames_asset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_video_frames_asset ON public.video_frames USING btree (asset_id);


--
-- Name: idx_video_frames_vec; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_video_frames_vec ON public.video_frames USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: idx_video_transcripts_asset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_video_transcripts_asset ON public.video_transcripts USING btree (asset_id);


--
-- Name: idx_video_transcripts_vec; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_video_transcripts_vec ON public.video_transcripts USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: idx_website_pages_analyzed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_website_pages_analyzed ON public.website_pages USING btree (ai_analyzed_status);


--
-- Name: idx_website_pages_domain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_website_pages_domain ON public.website_pages USING btree (domain);


--
-- Name: idx_website_pages_embedded; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_website_pages_embedded ON public.website_pages USING btree (embedded_status);


--
-- Name: idx_website_pages_fetched; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_website_pages_fetched ON public.website_pages USING btree (fetched_status);


--
-- Name: instagram_metrics_snapshots_post_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX instagram_metrics_snapshots_post_time_idx ON public.instagram_metrics_snapshots USING btree (post_id, captured_at DESC);


--
-- Name: instagram_post_assets_asset_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX instagram_post_assets_asset_idx ON public.instagram_post_assets USING btree (asset_id);


--
-- Name: instagram_post_assets_post_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX instagram_post_assets_post_idx ON public.instagram_post_assets USING btree (post_id);


--
-- Name: instagram_posts_account_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX instagram_posts_account_idx ON public.instagram_posts USING btree (account_username);


--
-- Name: instagram_posts_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX instagram_posts_embedding_idx ON public.instagram_posts USING ivfflat (caption_embedding public.vector_cosine_ops) WITH (lists='50');


--
-- Name: instagram_posts_timestamp_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX instagram_posts_timestamp_idx ON public.instagram_posts USING btree (timestamp_posted DESC NULLS LAST);


--
-- Name: instagram_video_scenes_asset_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX instagram_video_scenes_asset_idx ON public.instagram_video_scenes USING btree (asset_id);


--
-- Name: instagram_video_scenes_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX instagram_video_scenes_embedding_idx ON public.instagram_video_scenes USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='50');


--
-- Name: instagram_video_scenes_post_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX instagram_video_scenes_post_idx ON public.instagram_video_scenes USING btree (post_id);


--
-- Name: producer_assets_archetype_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX producer_assets_archetype_idx ON public.producer_assets USING btree (archetype) WHERE (archetype IS NOT NULL);


--
-- Name: producer_assets_category_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX producer_assets_category_idx ON public.producer_assets USING btree (category);


--
-- Name: producer_assets_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX producer_assets_embedding_idx ON public.producer_assets USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='20');


--
-- Name: producer_assets_kind_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX producer_assets_kind_idx ON public.producer_assets USING btree (asset_kind);


--
-- Name: producer_assets_lru_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX producer_assets_lru_idx ON public.producer_assets USING btree (archetype, last_used_at NULLS FIRST) WHERE (enabled AND (archetype IS NOT NULL));


--
-- Name: producer_assets_source_frame_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX producer_assets_source_frame_uq ON public.producer_assets USING btree (source_frame_id) WHERE (source_frame_id IS NOT NULL);


--
-- Name: yas_animations_gen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX yas_animations_gen ON public.yas_animations USING btree (yas_generation_id, created_at DESC);


--
-- Name: yas_animations_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX yas_animations_pending ON public.yas_animations USING btree (veo_operation_name) WHERE (status = 'generating'::text);


--
-- Name: yas_animations_poster_gen_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX yas_animations_poster_gen_idx ON public.yas_animations USING btree (poster_generation_id, created_at DESC) WHERE (gen_source = 'yas_poster_generations'::text);


--
-- Name: yas_generations_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX yas_generations_recent ON public.yas_generations USING btree (created_at DESC);


--
-- Name: yas_generations_th_parent_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX yas_generations_th_parent_idx ON public.yas_generations USING btree (((metadata ->> 'parent_talking_head_id'::text))) WHERE (metadata ? 'parent_talking_head_id'::text);


--
-- Name: yas_generations_th_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX yas_generations_th_status_idx ON public.yas_generations USING btree (((metadata ->> 'composition_status'::text))) WHERE (mode = 'talking_head_preview'::text);


--
-- Name: assets assets_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER assets_set_updated_at BEFORE UPDATE ON public.assets FOR EACH ROW EXECUTE FUNCTION public.trg_set_updated_at();


--
-- Name: brand_documents brand_documents_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER brand_documents_set_updated_at BEFORE UPDATE ON public.brand_documents FOR EACH ROW EXECUTE FUNCTION public.trg_set_updated_at();


--
-- Name: brand_profile_settings brand_profile_settings_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER brand_profile_settings_set_updated_at BEFORE UPDATE ON public.brand_profile_settings FOR EACH ROW EXECUTE FUNCTION public.trg_set_updated_at();


--
-- Name: instagram_posts instagram_posts_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER instagram_posts_updated_at BEFORE UPDATE ON public.instagram_posts FOR EACH ROW EXECUTE FUNCTION public.instagram_posts_set_updated_at();


--
-- Name: producer_assets producer_assets_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER producer_assets_updated_at BEFORE UPDATE ON public.producer_assets FOR EACH ROW EXECUTE FUNCTION public.producer_assets_set_updated_at();


--
-- Name: producer_compositions tg_producer_compositions_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER tg_producer_compositions_updated BEFORE UPDATE ON public.producer_compositions FOR EACH ROW EXECUTE FUNCTION public.tg_producer_set_updated_at();


--
-- Name: producer_ideas tg_producer_ideas_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER tg_producer_ideas_updated BEFORE UPDATE ON public.producer_ideas FOR EACH ROW EXECUTE FUNCTION public.tg_producer_set_updated_at();


--
-- Name: producer_references tg_producer_references_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER tg_producer_references_updated BEFORE UPDATE ON public.producer_references FOR EACH ROW EXECUTE FUNCTION public.tg_producer_set_updated_at();


--
-- Name: producer_runs tg_producer_runs_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER tg_producer_runs_updated BEFORE UPDATE ON public.producer_runs FOR EACH ROW EXECUTE FUNCTION public.tg_producer_set_updated_at();


--
-- Name: producer_scripts tg_producer_scripts_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER tg_producer_scripts_updated BEFORE UPDATE ON public.producer_scripts FOR EACH ROW EXECUTE FUNCTION public.tg_producer_set_updated_at();


--
-- Name: producer_videos tg_producer_videos_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER tg_producer_videos_updated BEFORE UPDATE ON public.producer_videos FOR EACH ROW EXECUTE FUNCTION public.tg_producer_set_updated_at();


--
-- Name: website_pages website_pages_set_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER website_pages_set_updated_at BEFORE UPDATE ON public.website_pages FOR EACH ROW EXECUTE FUNCTION public.trg_set_updated_at();


--
-- Name: assets assets_page_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_page_id_fkey FOREIGN KEY (page_id) REFERENCES public.website_pages(id) ON DELETE CASCADE;


--
-- Name: brand_profile_artifacts brand_profile_artifacts_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.brand_profile_artifacts
    ADD CONSTRAINT brand_profile_artifacts_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.brand_profile_runs(id) ON DELETE CASCADE;


--
-- Name: general_trend_sightings general_trend_sightings_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.general_trend_sightings
    ADD CONSTRAINT general_trend_sightings_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.general_trend_runs(id) ON DELETE SET NULL;


--
-- Name: general_trend_sightings general_trend_sightings_trend_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.general_trend_sightings
    ADD CONSTRAINT general_trend_sightings_trend_id_fkey FOREIGN KEY (trend_id) REFERENCES public.general_trends(id) ON DELETE CASCADE;


--
-- Name: image_embeddings image_embeddings_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.image_embeddings
    ADD CONSTRAINT image_embeddings_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: instagram_metrics_snapshots instagram_metrics_snapshots_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_metrics_snapshots
    ADD CONSTRAINT instagram_metrics_snapshots_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.instagram_posts(id) ON DELETE CASCADE;


--
-- Name: instagram_post_assets instagram_post_assets_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_post_assets
    ADD CONSTRAINT instagram_post_assets_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: instagram_post_assets instagram_post_assets_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_post_assets
    ADD CONSTRAINT instagram_post_assets_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.instagram_posts(id) ON DELETE CASCADE;


--
-- Name: instagram_video_scenes instagram_video_scenes_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_video_scenes
    ADD CONSTRAINT instagram_video_scenes_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: instagram_video_scenes instagram_video_scenes_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_video_scenes
    ADD CONSTRAINT instagram_video_scenes_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.instagram_posts(id) ON DELETE CASCADE;


--
-- Name: instagram_video_scenes instagram_video_scenes_representative_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instagram_video_scenes
    ADD CONSTRAINT instagram_video_scenes_representative_frame_id_fkey FOREIGN KEY (representative_frame_id) REFERENCES public.video_frames(id) ON DELETE SET NULL;


--
-- Name: producer_assets producer_assets_source_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_assets
    ADD CONSTRAINT producer_assets_source_asset_id_fkey FOREIGN KEY (source_asset_id) REFERENCES public.assets(id) ON DELETE SET NULL;


--
-- Name: producer_assets producer_assets_source_frame_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_assets
    ADD CONSTRAINT producer_assets_source_frame_id_fkey FOREIGN KEY (source_frame_id) REFERENCES public.video_frames(id) ON DELETE SET NULL;


--
-- Name: producer_assets producer_assets_source_post_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_assets
    ADD CONSTRAINT producer_assets_source_post_id_fkey FOREIGN KEY (source_post_id) REFERENCES public.instagram_posts(id) ON DELETE SET NULL;


--
-- Name: producer_campaign_items producer_campaign_items_campaign_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_campaign_items
    ADD CONSTRAINT producer_campaign_items_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES public.producer_campaigns(id) ON DELETE CASCADE;


--
-- Name: producer_compositions producer_compositions_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_compositions
    ADD CONSTRAINT producer_compositions_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.producer_runs(id) ON DELETE CASCADE;


--
-- Name: producer_feedback producer_feedback_brand_profile_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_feedback
    ADD CONSTRAINT producer_feedback_brand_profile_run_id_fkey FOREIGN KEY (brand_profile_run_id) REFERENCES public.brand_profile_runs(id);


--
-- Name: producer_ideas producer_ideas_parent_idea_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_ideas
    ADD CONSTRAINT producer_ideas_parent_idea_id_fkey FOREIGN KEY (parent_idea_id) REFERENCES public.producer_ideas(id);


--
-- Name: producer_ideas producer_ideas_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_ideas
    ADD CONSTRAINT producer_ideas_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.producer_runs(id) ON DELETE CASCADE;


--
-- Name: producer_references producer_references_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_references
    ADD CONSTRAINT producer_references_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.producer_runs(id) ON DELETE CASCADE;


--
-- Name: producer_runs producer_runs_brand_profile_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_runs
    ADD CONSTRAINT producer_runs_brand_profile_run_id_fkey FOREIGN KEY (brand_profile_run_id) REFERENCES public.brand_profile_runs(id);


--
-- Name: producer_scripts producer_scripts_idea_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_scripts
    ADD CONSTRAINT producer_scripts_idea_id_fkey FOREIGN KEY (idea_id) REFERENCES public.producer_ideas(id) ON DELETE CASCADE;


--
-- Name: producer_scripts producer_scripts_parent_script_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_scripts
    ADD CONSTRAINT producer_scripts_parent_script_id_fkey FOREIGN KEY (parent_script_id) REFERENCES public.producer_scripts(id);


--
-- Name: producer_videos producer_videos_parent_video_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_videos
    ADD CONSTRAINT producer_videos_parent_video_id_fkey FOREIGN KEY (parent_video_id) REFERENCES public.producer_videos(id);


--
-- Name: producer_videos producer_videos_script_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.producer_videos
    ADD CONSTRAINT producer_videos_script_id_fkey FOREIGN KEY (script_id) REFERENCES public.producer_scripts(id) ON DELETE CASCADE;


--
-- Name: text_chunks text_chunks_page_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.text_chunks
    ADD CONSTRAINT text_chunks_page_id_fkey FOREIGN KEY (page_id) REFERENCES public.website_pages(id) ON DELETE CASCADE;


--
-- Name: trend_posts trend_posts_competitor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trend_posts
    ADD CONSTRAINT trend_posts_competitor_id_fkey FOREIGN KEY (competitor_id) REFERENCES public.trend_competitors(id) ON DELETE CASCADE;


--
-- Name: video_frames video_frames_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_frames
    ADD CONSTRAINT video_frames_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: video_summaries video_summaries_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_summaries
    ADD CONSTRAINT video_summaries_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: video_transcripts video_transcripts_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_transcripts
    ADD CONSTRAINT video_transcripts_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: yas_animations yas_animations_yas_generation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_animations
    ADD CONSTRAINT yas_animations_yas_generation_id_fkey FOREIGN KEY (yas_generation_id) REFERENCES public.yas_generations(id) ON DELETE CASCADE;


--
-- Name: yas_generations yas_generations_parent_generation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_generations
    ADD CONSTRAINT yas_generations_parent_generation_id_fkey FOREIGN KEY (parent_generation_id) REFERENCES public.yas_generations(id) ON DELETE SET NULL;


--
-- Name: yas_generations yas_generations_student_upload_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yas_generations
    ADD CONSTRAINT yas_generations_student_upload_id_fkey FOREIGN KEY (student_upload_id) REFERENCES public.yas_uploads(id) ON DELETE SET NULL;


--
-- Name: assets; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.assets ENABLE ROW LEVEL SECURITY;

--
-- Name: brand_documents; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.brand_documents ENABLE ROW LEVEL SECURITY;

--
-- Name: brand_profile_artifacts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.brand_profile_artifacts ENABLE ROW LEVEL SECURITY;

--
-- Name: brand_profile_runs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.brand_profile_runs ENABLE ROW LEVEL SECURITY;

--
-- Name: brand_profile_settings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.brand_profile_settings ENABLE ROW LEVEL SECURITY;

--
-- Name: image_embeddings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.image_embeddings ENABLE ROW LEVEL SECURITY;

--
-- Name: instagram_metrics_snapshots; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.instagram_metrics_snapshots ENABLE ROW LEVEL SECURITY;

--
-- Name: instagram_post_assets; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.instagram_post_assets ENABLE ROW LEVEL SECURITY;

--
-- Name: instagram_posts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.instagram_posts ENABLE ROW LEVEL SECURITY;

--
-- Name: instagram_video_scenes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.instagram_video_scenes ENABLE ROW LEVEL SECURITY;

--
-- Name: page_links; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.page_links ENABLE ROW LEVEL SECURITY;

--
-- Name: producer_assets; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.producer_assets ENABLE ROW LEVEL SECURITY;

--
-- Name: producer_compositions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.producer_compositions ENABLE ROW LEVEL SECURITY;

--
-- Name: producer_feedback; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.producer_feedback ENABLE ROW LEVEL SECURITY;

--
-- Name: producer_ideas; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.producer_ideas ENABLE ROW LEVEL SECURITY;

--
-- Name: producer_references; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.producer_references ENABLE ROW LEVEL SECURITY;

--
-- Name: producer_runs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.producer_runs ENABLE ROW LEVEL SECURITY;

--
-- Name: producer_scripts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.producer_scripts ENABLE ROW LEVEL SECURITY;

--
-- Name: producer_videos; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.producer_videos ENABLE ROW LEVEL SECURITY;

--
-- Name: scrape_runs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.scrape_runs ENABLE ROW LEVEL SECURITY;

--
-- Name: text_chunks; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.text_chunks ENABLE ROW LEVEL SECURITY;

--
-- Name: video_frames; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.video_frames ENABLE ROW LEVEL SECURITY;

--
-- Name: video_summaries; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.video_summaries ENABLE ROW LEVEL SECURITY;

--
-- Name: video_transcripts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.video_transcripts ENABLE ROW LEVEL SECURITY;

--
-- Name: website_pages; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.website_pages ENABLE ROW LEVEL SECURITY;

--
-- Name: yas_animations; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.yas_animations ENABLE ROW LEVEL SECURITY;

--
-- PostgreSQL database dump complete
--


COMMIT;
