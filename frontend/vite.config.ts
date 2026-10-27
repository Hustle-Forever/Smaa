import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * The API port, read from the same place the backend reads it.
 *
 * backend/app/config.py takes API_PORT from the project-root .env, so hardcoding
 * 8000 here meant the proxy silently pointed at nothing whenever that file said
 * otherwise. Precedence matches python-dotenv's: a real environment variable
 * wins, then the .env, then 8000.
 */
function apiPort(): string {
  if (process.env.API_PORT) return process.env.API_PORT;
  try {
    const env = readFileSync(fileURLToPath(new URL("../.env", import.meta.url)), "utf8");
    const hit = env.match(/^\s*API_PORT\s*=\s*(\d+)\s*$/m);
    if (hit) return hit[1];
  } catch {
    // No .env (a fresh clone, or CI). 8000 is the backend's own default.
  }
  return "8000";
}

/**
 * Optional: read a deployed studio instead of the local backend, e.g.
 *   API_TARGET=https://api.42studio.fit PORT=4243 npm run dev
 * so a UI change can be reviewed against real data. Sign in on the page as
 * usual: the session cookie has no domain, so it lands on this dev server.
 * Careful: every button then acts on that deployment.
 */
const API_TARGET = process.env.API_TARGET?.replace(/\/$/, "");

// The Video Editor shares its Remotion composition with the compose service so
// the in-browser <Player> preview renders the EXACT same component the server
// exports (WYSIWYG). These aliases let the frontend import that file (which
// lives outside frontend/src) by a stable name.
const videoEditComposition = fileURLToPath(
  new URL("../services/remotion/src/compositions/VideoEdit.tsx", import.meta.url),
);
const videoEditSchema = fileURLToPath(
  new URL("../services/remotion/src/compositions/schema.ts", import.meta.url),
);

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "video-edit-composition": videoEditComposition,
      "video-edit-schema": videoEditSchema,
    },
    // Force a SINGLE copy of these so the shared composition (resolved from the
    // remotion service's tree) and the frontend share one React/Remotion
    // instance — mixing two copies breaks hooks/context.
    dedupe: [
      "react",
      "react-dom",
      "remotion",
      "@remotion/player",
      "@remotion/transitions",
      "@remotion/fonts",
      "@remotion/google-fonts",
    ],
  },
  server: {
    host: "127.0.0.1",
    port: Number(process.env.PORT) || 4242,
    strictPort: true,
    // Let a Cloudflare quick tunnel (`cloudflared tunnel --url http://localhost:4242`)
    // reach the dev server — Vite otherwise rejects the *.trycloudflare.com Host
    // header with "Blocked request. This host is not allowed." Harmless for local dev.
    allowedHosts: [".trycloudflare.com"],
    // When sharing over the tunnel (TUNNEL=1), the HMR websocket must ride the
    // tunnel's public port (443/wss), not localhost:4242 — otherwise the friend's
    // browser logs ws-connect errors. Left untouched for normal local dev.
    ...(process.env.TUNNEL === "1"
      ? { hmr: { clientPort: 443, protocol: "wss" as const } }
      : {}),
    // Allow serving the shared composition file from the repo root (outside frontend/).
    fs: { allow: [".."] },
    proxy: {
      // Forward API calls to FastAPI so the browser sees same-origin and SSE
      // works without CORS gymnastics. Both `/api/*` and the SSE streams
      // share this proxy.
      "/api": {
        target: API_TARGET || `http://127.0.0.1:${apiPort()}`,
        // A remote target checks the Host header; the local one never did.
        changeOrigin: !!API_TARGET,
        secure: true,
        ws: false,
        timeout: 300000, // 5 min — covers slow Gemini Pro / OpenAI quality=high
      },
      // Media objects, for when S3_PUBLIC_BASE_URL points at this dev server.
      // The local compose Caddy (docker-compose.local.yml) owns the MinIO route
      // and sets the Host the URL was signed for, so this only forwards. Nothing
      // in the app lives under /media, so this shadows no page.
      "/media": {
        target: "http://127.0.0.1:8088",
        changeOrigin: false,
      },
    },
  },
  build: {
    target: "es2020",
    sourcemap: true,
  },
});
