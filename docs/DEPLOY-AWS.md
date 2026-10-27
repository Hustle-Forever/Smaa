# Deploying the `aws-deploy` branch (single EC2 host)

The whole app on one box at **https://api.42studio.fit**: the SPA, the API, the
Remotion renderer, Postgres and MinIO. No Supabase: the database is a container
and media objects live in MinIO, an S3-compatible store in another container.

```
            api.42studio.fit  :80 / :443
                    |
             +------v------+    /media/*     +-------------+
             | web (Caddy) |---------------->|    minio    |  studio42_minio
             +------+------+  GET/HEAD only  +------^------+
                    | /api/*                        |
             +------v------+      +-------------+   | S3 API, minio:9000
             |   backend   |<---->|  remotion   |   |
             |  FastAPI    |      |  renderer   |   |
             +--+---+---+--+      +------+------+   |
                |   |   |  studio42_data |          |
                |   |   +----------------+          |
                |   +-------------------------------+
         +------v------+
         |  postgres   |  pgvector pg17, studio42_db
         +-------------+
```

Caddy terminates TLS, serves the static SPA, proxies `/api/*` to the backend
and `/media/*` to MinIO. A one-shot `minio-init` container (not drawn) prepares
MinIO on every `up`.

| Host | EC2 `i-0bbce1391ead52b1d`, m7i.xlarge, eu-north-1, Elastic IP 13.48.141.102 |
|---|---|
| Checkout | `/home/ec2-user/42-studio`, branch `aws-deploy` (read-only deploy key, ssh alias `github-42studio`) |
| Config | `.env` at the repo root (compose reads it for `${...}` and passes it to the backend) + `secrets/gcp-sa.json` |

## How media is stored

- Four buckets: `producer-assets`, `producer-media` and `brand-docs` are
  private; `approved-instagram` is readable by anyone who knows an object's
  URL (Instagram fetches from it), but it cannot be listed.
- The browser gets `https://api.42studio.fit/media/<bucket>/<key>` URLs.
  Private ones carry a presigned `X-Amz-...` query (valid up to 7 days). Caddy
  forwards only object GET/HEAD to MinIO; listings, MinIO's admin paths and any
  write through `/media` get a 404.
- The backend signs with its own least-privilege user (`S3_ACCESS_KEY`), never
  the root account, because the access key is visible in every presigned URL.
  `minio-init` creates the buckets, the public-read policy and that user from
  `.env`, using the root account.
- New image assets get a stored 480px WebP thumbnail next to the original
  (`thumbs/<key>.webp`) for the asset grids.
- `studio42_data` still holds the local working files: renders Remotion reads,
  download caches, and everything written before MinIO. If MinIO can't be
  reached, an upload falls back to that disk (logged as a warning) and its row
  records where the bytes went.

## Volumes

Fixed names, so they never depend on the directory or project name:

| Volume | Holds |
|---|---|
| `studio42_db` | Postgres data |
| `studio42_minio` | MinIO: every bucket object, plus MinIO's users and policies |
| `studio42_data` | local working files: renders, caches, and every file written before MinIO (`/app/backend/data`) |
| `studio42_caddy_data` / `studio42_caddy_config` | the Let's Encrypt certificate: keep it, or every restart re-issues (5/week limit) |

`docker compose down` keeps all of them. Never add `-v`.

## `.env`: what differs from local dev

| Variable | Value | Why |
|---|---|---|
| `DOMAIN` / `PUBLIC_URL` | `api.42studio.fit` / `https://api.42studio.fit` | Caddy's certificate name; CORS + email links; media URLs are `<PUBLIC_URL>/media/...`, so no trailing `/` |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | `studio` / `studio` / hex | create the database on first boot and connect the backend (hex only: the DSN is built by concatenation) |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | hex | MinIO's admin account, used only by `minio-init` |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | hex, a different user from the root one | the backend's own MinIO user |
| `SUPABASE_*` | empty | no Supabase on this host |
| `APP_ACCESS_PASSWORD` | random hex | arms the sign-in gate and signs the cookie |
| `APP_LOGIN_PASSWORD` | the password people type | shared by the accounts in `backend/app/access.py` |
| `STUDIO_INSTANCE_ID` | `prod` | render ownership |

`STORAGE_BACKEND=s3`, `S3_ENDPOINT=http://minio:9000`,
`S3_PUBLIC_BASE_URL=<PUBLIC_URL>/media` and `S3_REGION=us-east-1` are pinned in
`docker-compose.yml`, so they are not set in `.env`.

### Adding the MinIO lines

Run once on the server. The heredoc fills in fresh random values:

```bash
cd ~/42-studio
grep -c '^MINIO_ROOT_USER=' .env    # must print 0; if not, the lines are already there
cat >> .env <<EOF

# MinIO media storage (see docs/DEPLOY-AWS.md). Root = admin only (minio-init);
# the backend signs with S3_ACCESS_KEY.
MINIO_ROOT_USER=$(openssl rand -hex 8)
MINIO_ROOT_PASSWORD=$(openssl rand -hex 32)
S3_ACCESS_KEY=$(openssl rand -hex 10)
S3_SECRET_KEY=$(openssl rand -hex 32)
EOF
grep -E '^(MINIO_ROOT_USER|MINIO_ROOT_PASSWORD|S3_ACCESS_KEY|S3_SECRET_KEY)=.' .env | cut -d= -f1
```

The last command should list the four names (and not their values). If any of
the four is missing, `docker compose` refuses to start with
`required variable ... is missing a value`.

## First deploy (empty server)

```bash
cd ~/42-studio
git fetch && git checkout aws-deploy && git pull
# .env: copy backend/.env.example + .env.deploy.example, fill them in, and add
# the MinIO lines as shown above
docker compose build
docker compose up -d postgres          # loads db/schema.sql into the empty volume
docker compose ps postgres             # wait for (healthy)

# Optional: seed from Supabase (see the docstring in db/seed_from_supabase.py)
docker compose up -d
docker compose ps -a                   # minio (healthy), minio-init Exited (0)
curl https://api.42studio.fit/api/health
```

## Switching the running server to MinIO (one time)

The server ran with every file on local disk until now. Do these in order.

**1. Before deploying, decide on approved campaign items.** Once media can be
hosted, the campaign poller (every 15 s) hosts and books up to 25 approved
items on its first tick, even ones whose `proposed_time` is already past.
Read-only check on the running stack:

```bash
docker exec -i studio-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT id, status, proposed_time FROM producer_campaign_items WHERE status = 'approved';
SQL
```

For every row that should not go out, press **Cancel** on it in Campaigns
before you deploy. Separately, pending scheduled post #1 has no hosted media
yet: nothing hosts it on its own, but when someone presses **Post** on it, its
media is hosted and it publishes immediately.

**2. Add the four MinIO lines to `.env`** (see "Adding the MinIO lines" above).

**3. Deploy.** The web image must be rebuilt, because the Caddyfile with the
new `/media` route is baked into it; `--build` does that.

```bash
cd ~/42-studio && git pull && docker compose up -d --build
docker compose ps -a                                  # minio (healthy), minio-init Exited (0)
docker compose logs minio-init | tail -3              # "...buckets, public policy and app user are in place"
docker compose logs backend | grep 'Storage backend'  # Storage backend: s3
```

**4. Check the edge.**

```bash
curl -s https://api.42studio.fit/api/health
curl -s -o /dev/null -w '%{http_code}\n' https://api.42studio.fit/media/minio/health/live         # 404
curl -s -o /dev/null -w '%{http_code}\n' https://api.42studio.fit/media/approved-instagram/       # 404
```

In the app, Settings > Instagram should now report ready (it also needs the
Instagram token, which this server already has). Upload an image in Assets:
it shows in the grid, and the browser's network tab shows its bytes coming
from `https://api.42studio.fit/media/producer-assets/...`.

**5. Copy the existing local files into MinIO (backfill).** The script ships
in the backend image, so it is there after step 3's `--build`. Take the
database dump from "Backup" below first. Then a dry run: it only reads, and
prints a per-table plan with totals, every missing file and every key it had
to rewrite:

```bash
docker compose exec backend python -m scripts.backfill_local_to_s3
docker compose exec backend python -m scripts.backfill_local_to_s3 --apply
# or a slice first: --table producer_assets --limit 5
# (--limit counts per file column: frame and video of one table count apart)
```

It is idempotent (rows already in a bucket are skipped), so re-run it after any
failure; exit code 1 means some rows failed and a later re-run picks them up.
Each row keeps its `local_path`, so Remotion keeps working and the backfill can
be undone, which is also why the `studio42_data` volume must stay. Asset rows
also get their stored thumbnail here. To undo it for one row, set its bucket
and key columns back to NULL: `storage_bucket` and `storage_path`, or the
`frame_storage_*` and `video_storage_*` pairs in `yas_animation_generations`
and `yas_general_video_generations`. A rollback must undo it for every row
first (see "Rolling back").

## Update

```bash
cd ~/42-studio && git pull && docker compose up -d --build && docker compose ps -a
```

`minio-init` runs again on every `up`. Every step it takes is idempotent.

## Backup

```bash
docker exec studio-db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > db-$(date +%F).sql.gz
docker run --rm -v studio42_data:/v:ro -v "$PWD":/b alpine tar czf /b/data-$(date +%F).tar.gz -C /v .

# MinIO: stop it (and the backend, so no upload falls back to local disk
# meanwhile), copy the volume, start everything again. /media is down for
# the duration.
docker compose stop backend minio
docker run --rm -v studio42_minio:/v:ro -v "$PWD":/b alpine tar czf /b/minio-$(date +%F).tar.gz -C /v .
docker compose up -d
```

The volume copy is the complete backup: objects with their Content-Type,
Cache-Control and Content-Disposition, plus the bucket policy and the app user.
To restore it, extract it into an empty `studio42_minio` volume with MinIO
stopped. Do not use `mc mirror` to a folder as the backup. Restoring from it
loses Cache-Control, and loses the forced download on SVG/HTML files, which
then render inline from the app's origin. It also loses the users and policies.

## Rotating the MinIO keys

- `S3_SECRET_KEY`: change it in `.env`, then `docker compose up -d`.
  `minio-init` updates the user's secret, the backend is recreated with the new
  one, and the old secret stops working. Presigned URLs already handed out
  stop working too. `minio-init` also sets the three private buckets back to
  private. If you rotate because the secret may have leaked, also check the
  access keys below.
- Access keys of the app user. The `studio-app` policy denies the app user
  every admin action, so it cannot create access keys for itself. A key made
  before that policy was in place (MinIO allows it unless denied) keeps
  working after a rotation, with the app user's rights, until you remove it.
  List the keys and remove any you did not create:

  ```bash
  docker compose run --rm --no-deps --entrypoint sh minio-init -c \
    'mc alias set studio http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc admin accesskey ls studio "$S3_ACCESS_KEY"'
  docker compose run --rm --no-deps --entrypoint sh minio-init -c \
    'mc alias set studio http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc admin accesskey rm studio <access key>'
  ```
- `MINIO_ROOT_PASSWORD`: change it in `.env`, then `docker compose up -d`.
  MinIO restarts with the new password and keeps its data, users and policies.
- `S3_ACCESS_KEY`: changing it creates a new user with the same rights. The
  old one keeps working until you remove it:

  ```bash
  docker compose run --rm --no-deps --entrypoint sh minio-init -c \
    'mc alias set studio http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc admin user rm studio <old access key>'
  ```

## Rolling back

The previous commit reads a row from its bucket whenever the row names one,
and with no Supabase on this host that read fails without trying the row's
`local_path`. So if the backfill (step 5) has run, undo it BEFORE the old code
starts, or every backfilled poster, poster+human and animation fails in the
editing actions (variants, remove-logo, save to assets, animate) although its
file is still on disk. The current code reads the local copy once the bucket
columns are empty, so run the undo while it is still up. It only resets rows
that still have a local copy:

```bash
docker exec -i studio-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
BEGIN;
UPDATE producer_assets SET storage_bucket = NULL, storage_path = NULL
  WHERE storage_bucket IS NOT NULL AND local_path IS NOT NULL AND local_path <> '';
UPDATE yas_generations SET storage_bucket = NULL, storage_path = NULL
  WHERE storage_bucket IS NOT NULL AND local_path IS NOT NULL AND local_path <> '';
UPDATE yas_poster_generations SET storage_bucket = NULL, storage_path = NULL
  WHERE storage_bucket IS NOT NULL AND local_path IS NOT NULL AND local_path <> '';
UPDATE yas_animations SET storage_bucket = NULL, storage_path = NULL
  WHERE storage_bucket IS NOT NULL AND local_path IS NOT NULL AND local_path <> '';
UPDATE yas_animation_generations SET frame_storage_bucket = NULL, frame_storage_path = NULL
  WHERE frame_storage_bucket IS NOT NULL AND frame_local_path IS NOT NULL AND frame_local_path <> '';
UPDATE yas_animation_generations SET video_storage_bucket = NULL, video_storage_path = NULL
  WHERE video_storage_bucket IS NOT NULL AND video_local_path IS NOT NULL AND video_local_path <> '';
UPDATE yas_general_video_generations SET frame_storage_bucket = NULL, frame_storage_path = NULL
  WHERE frame_storage_bucket IS NOT NULL AND frame_local_path IS NOT NULL AND frame_local_path <> '';
UPDATE yas_general_video_generations SET video_storage_bucket = NULL, video_storage_path = NULL
  WHERE video_storage_bucket IS NOT NULL AND video_local_path IS NOT NULL AND video_local_path <> '';
COMMIT;
SQL
```

Then check out the previous commit and `docker compose up -d --build`. The
reset rows are served from local disk again. Anything uploaded while MinIO was
on exists only in MinIO, so it is unavailable until you switch back. Keep
`studio42_minio`. To switch back, deploy this branch again and re-run the
backfill (step 5): it uploads the reset rows again.

## Known limits of the self-hosted setup

- **Public Instagram copies stay public until deleted.** Booked media is
  copied to `approved-instagram` under an unguessable key (128 random bits).
  Cancelling a post (in the Library's Scheduled queue, or a booked item in
  Campaigns), deleting its campaign, or deleting the asset deletes those
  copies. Published posts keep theirs. A copy left behind some other way (for
  example a delete that failed while MinIO was down) stays reachable at its
  URL until someone deletes it.
- **No on-the-fly image resizing.** Grids use the stored 480px thumbnails. Rows
  from before MinIO get theirs from the backfill; until it has run, the
  Background tab downloads full-size files.
- **Media URLs change on every backend restart.** Presigned URLs are cached
  in the backend process only, so after each deploy browsers download media
  they had cached once more.
- **One disk, one MinIO.** No replication: the backup above is the only other
  copy. The image is a community build (`pgsty/minio`, pinned tag), because the
  official MinIO images can no longer be pulled anonymously.
- **Single instance.** Renders are owned by `STUDIO_INSTANCE_ID` and live on this
  disk. Do not run a second copy against the same database.
