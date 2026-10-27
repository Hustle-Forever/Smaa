# 42 Studio — web edge (static SPA + TLS + /api reverse proxy via Caddy)
#
# Build context is the REPO ROOT (not frontend/) because frontend/vite.config.ts
# aliases two files out of services/remotion/src/compositions (so the in-browser
# <Player> renders the exact same composition the server exports). We replicate
# the dev layout (frontend/ next to services/remotion/) and run npm ci in BOTH
# so `tsc -b && vite build` resolves types identically to the dev machine.

# ---------- Stage 1: build the static bundle --------------------------------
FROM node:22-bookworm-slim AS build
WORKDIR /app

# Dependency layers (cached unless lockfiles change).
COPY frontend/package.json frontend/package-lock.json ./frontend/
COPY services/remotion/package.json services/remotion/package-lock.json ./services/remotion/
RUN cd frontend && npm ci
RUN cd services/remotion && npm ci

# Sources. services/remotion FIRST so the alias target exists before the build.
COPY services/remotion ./services/remotion
COPY frontend ./frontend

RUN cd frontend && npm run build

# ---------- Stage 2: serve with Caddy (auto-HTTPS) --------------------------
FROM caddy:2-alpine
COPY --from=build /app/frontend/dist /srv
COPY web/Caddyfile /etc/caddy/Caddyfile

EXPOSE 80 443
