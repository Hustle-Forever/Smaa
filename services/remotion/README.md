# Studio Remotion service

Express HTTP service that bundles and renders Remotion compositions. Used by
the Python producer agent to stitch parent + extension Veo clips into one
continuous MP4.

## Why a separate service?

Remotion is Node + React. The Studio backend is Python. Keeping Remotion in
its own folder avoids dragging a Node runtime into the FastAPI process. The
two services share `backend/data/producer/` on disk so file handoff is
free — Veo writes MP4s here, Remotion reads them, writes the composed MP4
back here, and FastAPI serves it via the existing video-file route.

## Run

```bash
cd services/remotion
npm install            # first time only
npm run dev            # starts on :3001 with hot reload
```

In another terminal you should also have:
```bash
cd backend
python run.py          # FastAPI on :8000
```

```bash
cd frontend
npm run dev            # Vite on :4242
```

## Endpoints

| Method | Path | Body |
|---|---|---|
| GET  | `/health` | — |
| POST | `/render` | `{ template, output_path, props, format? }` |
| POST | `/still` | `{ template, output_path, props, frame }` |
| GET  | `/jobs/:id` | — |
| GET  | `/files/<run>/<file>` | static MP4s under `backend/data/producer/` |

`POST /render` returns `{ job_id }` immediately; the composition_poller on the
FastAPI side calls `GET /jobs/:id` every 15s and updates the DB row.

With `format: "png-sequence"` the job renders every frame as a transparent
PNG into the DIRECTORY `output_path` (must be under `backend/data/`), and the
finished job adds `frame_count`, `frame_pattern` (printf name, e.g.
`element-%03d.png`) and `start_number`. Used by `brand-overlay-42`, which draws
only graphics for the backend to overlay on untouched footage with ffmpeg.
Fully opaque frames are rewritten as RGBA so the sequence never mixes PNG
colour types (ffmpeg drops frames on a mixed sequence).

## Static assets

`public/` is bundled as Remotion's public dir (`staticFile()`), by the server
and by the CLI when run from this folder. `public/brand42/` holds the 42 brand
kit: VG5000 + Jost 400 (each with its OFL licence), the stacked logo, the
owner's five patterns as seamless one-colour tiles (`patterns/`, they slide
left to right through the bars and over the picture's left edge), and the
Phosphor topic icons' licence (their path data is `src/brand42/icons.ts`).
`scripts/make_brand42_assets.py` rebuilds all of it (see its docstring); a
render never needs the network.

## Iterate on templates

```bash
npm run studio
```

Opens the Remotion Studio in a browser. You can preview compositions with
the `defaultProps` from `src/Root.tsx` (currently using Big Buck Bunny clips
as a sanity test). When you're happy, the running `npm run dev` server has
already hot-reloaded the bundled output.

## Add a template

1. Create `src/compositions/MyTemplate.tsx` (export the component + a
   `calculateMetadata` function for dynamic duration/fps).
2. Register it in `src/Root.tsx`:
   ```tsx
   <Composition
     id="MyTemplate"
     component={MyTemplate}
     calculateMetadata={...}
     defaultProps={...}
     /* ... */
   />
   ```
3. FastAPI side: call `submit_render(template="MyTemplate", props=...)`.

## Caveats

- **In-memory job store.** If the Express process restarts mid-render, the
  job ID is lost and the FastAPI poller will mark the composition `failed`
  on its next tick. Future versions can persist to JSON or Postgres.
- **No queue.** Renders launch concurrently. For v4 (one-button stitch)
  contention is unlikely; add `p-queue` if it ever matters.
- **Bundle on boot.** The first `/render` waits for the initial bundle
  (~5-10s); subsequent renders reuse the same bundle. Re-bundling only
  happens on full restart.
- **Windows paths.** `output_path` should be absolute (e.g.
  `C:\Users\...\backend\data\producer\9\composed-1.mp4`). Express handles
  the platform separator correctly via `path` from Node stdlib.
