# 42 Studio — Remotion compose/render sidecar (Express + @remotion/renderer)
#
# @remotion/renderer drives a headless Chrome to render compositions, so the
# image needs Chrome's shared libraries. We pre-fetch Chrome Headless Shell at
# build time ("remotion browser ensure") so the first render isn't blocked on a
# download. TypeScript is executed directly by tsx — no compile step.
FROM node:22-bookworm-slim

# --- Chrome Headless Shell system libraries (Debian bookworm) ----------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-liberation \
        libnss3 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxext6 \
        libgbm1 libxkbcommon0 libpango-1.0-0 libcairo2 libasound2 \
        libatspi2.0-0 libxshmfence1 libdrm2 \
    && rm -rf /var/lib/apt/lists/*

ENV PORT=3001 \
    HOST=0.0.0.0

WORKDIR /app/services/remotion

# --- Node deps (cached layer) ----------------------------------------------
# devDependencies are kept: tsx (runtime) and @remotion/cli (browser ensure).
COPY package.json package-lock.json ./
RUN npm ci

# --- Service source ---------------------------------------------------------
COPY . .

# Bake the headless browser into the image (no runtime download).
RUN npx remotion browser ensure

EXPOSE 3001

# The shared backend/data tree is mounted at /app/backend/data at runtime so the
# service's relative path (__dirname/../../backend/data) resolves identically to
# the backend container.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD node -e "require('http').get('http://localhost:'+(process.env.PORT||3001)+'/health',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"

CMD ["npx", "tsx", "server.ts"]
