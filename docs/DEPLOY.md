# Deploying 42 Studio to a VPS (Docker)

This stack runs three containers behind Caddy (automatic HTTPS) and talks to a
**remote Supabase** Postgres — there is no database container.

```
Internet ─443/80─▶ web (Caddy: static SPA + TLS + /api proxy)
                      ├─/api/*─▶ backend (FastAPI :8000)
                      └ backend ─POST /render─▶ remotion (:3001)
        backend ⇄ remotion share one volume at /app/backend/data
        backend ─▶ Supabase Postgres + Vertex/Veo/Gemini + OpenAI + Instagram
```

## Domain topology (42studio.fit) — important

This app is **one public origin**, not three. Don't create separate `api.` or
`remotion.` subdomains:

- **`https://42studio.fit`** → the frontend (SPA), served by Caddy.
- **`https://42studio.fit/api/*`** → the backend, reverse-proxied by Caddy on the
  **same origin**. The frontend was built to call the API with relative `/api`
  paths, so it MUST live under the same hostname — a separate API domain would
  require frontend code changes and CORS. Keep it same-origin.
- **Remotion is internal only.** The backend reaches it over the private Docker
  network at `http://remotion:3001`; the browser never touches it. Do **not**
  expose Remotion publicly — it serves/render files and has no auth. It gets no
  domain and no published port.

So the only DNS you create is `42studio.fit` (and optionally `www`) → your VPS IP.

## 0. Prerequisites

- A VPS (Ubuntu/Debian recommended) with **Docker Engine + Compose v2**:
  ```bash
  curl -fsSL https://get.docker.com | sh
  ```
- The domain **42studio.fit** with DNS **A-records** for both the apex and
  `www` pointing at the VPS public IP (required for Let's Encrypt). Ports
  **80 and 443** open in the firewall.
- A **Google Cloud service-account JSON key** (see step 2).
- Sizing: the backend image is large (PyTorch + CLIP). Use a box with
  **≥ 4 GB RAM** and **≥ 20 GB disk**.

## 1. Get the code onto the VPS

```bash
git clone <your-repo-url> studio && cd studio
```

## 2. Google Cloud service-account key

In Google Cloud Console → IAM & Admin → Service Accounts:

1. Create a service account.
2. Grant it: **Vertex AI User** and **Storage Object Admin** on your
   `VEO_OUTPUT_BUCKET` (and ensure the Veo region is enabled).
3. Create a JSON key and download it.
4. Put it on the VPS at:
   ```bash
   cp /path/to/downloaded-key.json secrets/gcp-sa.json
   ```
   It's mounted read-only into the backend at `/secrets/gcp-sa.json`
   (`GOOGLE_APPLICATION_CREDENTIALS` already points there). The `secrets/`
   directory is gitignored.

## 3. App secrets — `backend/.env`

```bash
cp backend/.env.example backend/.env
```
Fill in at minimum:

- **Supabase Postgres:** `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`,
  `SUPABASE_URL`, `SUPABASE_PROJECT_REF`, `SUPABASE_SERVICE_ROLE_KEY`.
- **Vertex/Veo:** `GOOGLE_CLOUD_PROJECT`, `VEO_OUTPUT_BUCKET`
  (auth comes from the mounted key, **not** `gcloud` here).
- **OpenAI:** `OPENAI_API_KEY`.
- **Instagram / SMTP / ElevenLabs / Suno:** optional — fill to enable.
- **`APP_ACCESS_PASSWORD`** — set this **before** going public so every paid
  Vertex/OpenAI call sits behind the sign-in gate. Nobody types this value: it
  arms the gate and signs the session cookie. People sign in with an email and
  password from the accounts in `backend/app/access.py`.

> You do **not** need to change `API_HOST`, `AUTO_RELOAD`, or `REMOTION_BASE_URL`
> in `backend/.env` — compose overrides those for Docker automatically.

## 4. Deploy config — root `.env`

A ready-to-use `.env` is already committed-as-ignored with the real domain:

```
DOMAIN=42studio.fit www.42studio.fit
ACME_EMAIL=42abudhabiai@gmail.com
PUBLIC_URL=https://42studio.fit
```

If it's missing on the VPS (it's gitignored), recreate it from the template:
`cp .env.deploy.example .env`. Adjust `ACME_EMAIL` if you want cert notices
elsewhere.

## 5. Build & start

```bash
docker compose build      # first build is slow: pulls torch + CLIP + Chrome
docker compose up -d
docker compose ps         # all three should become healthy
```

Caddy obtains the TLS certificate automatically on first start (needs the DNS
A-record live and ports 80/443 reachable).

## 6. Smoke test

```bash
# Backend health (inside the network):
docker compose exec backend curl -sf http://localhost:8000/api/health

# Cross-container reach + remotion 0.0.0.0 bind:
docker compose exec backend curl -sf http://remotion:3001/health

# Shared volume is truly shared (same listing from both):
docker compose exec backend  ls /app/backend/data/producer
docker compose exec remotion ls /app/backend/data/producer
```

Then open `https://<DOMAIN>` — the app should load, you should hit the password
gate (if set), and an agent run should stream live progress (SSE).

## 7. Day-2 operations

```bash
# Logs
docker compose logs -f backend
docker compose logs -f remotion
docker compose logs -f web

# Update after a git pull
git pull
docker compose build
docker compose up -d

# Stop / start (data + certs persist in named volumes)
docker compose down
docker compose up -d
```

### Back up generated media

All rendered media lives in the `studio-data` named volume:

```bash
docker run --rm -v studio_studio-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/studio-data-backup.tar.gz -C /data .
```
(The volume name is prefixed with the compose project dir, usually `studio_`.
Confirm with `docker volume ls`.)

## Notes

- **Seeding existing media:** the volume starts empty. To carry over your local
  `backend/data`, either temporarily bind-mount it (replace the `studio-data`
  mount with `./backend/data:/app/backend/data` in both `backend` and
  `remotion`), or `docker cp` files into the running backend container once.
- **No GCP yet?** Leave `GOOGLE_CLOUD_PROJECT` unset in `backend/.env` — the
  Veo/Gemini pollers stay disabled and the rest of the app still runs.
- **Local (non-VPS) test without a domain:** set `DOMAIN=:80` and `PUBLIC_URL=
  http://localhost` in root `.env` — Caddy serves plain HTTP, no TLS.
- **Local dev is unchanged:** keep using `backend/start.ps1` + `npm run dev` +
  the remotion dev server; the Docker setup is additive.
