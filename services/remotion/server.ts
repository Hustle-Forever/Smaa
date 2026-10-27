/**
 * Remotion compose service — Express HTTP server.
 *
 * Receives render jobs from the FastAPI backend, runs them through
 * @remotion/renderer, writes the resulting MP4 to the shared filesystem.
 *
 * Endpoints:
 *   POST /render          { template, output_path, props }     → { job_id }
 *                         + format: "png-sequence" → output_path is a DIRECTORY
 *                           of transparent PNG frames (see runFramesInner)
 *   GET  /jobs/:id                                              → { status, progress, ... }
 *   GET  /files/<run>/<file>                                    → static MP4 (for Remotion to fetch clips)
 *   GET  /health                                                → { ok: true }
 */
import { bundle } from "@remotion/bundler";
import { renderFrames, renderMedia, renderStill, selectComposition } from "@remotion/renderer";
import cors from "cors";
import express from "express";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import zlib from "node:zlib";
import { nanoid } from "nanoid";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PORT = Number(process.env.PORT || 3001);
const DATA_DIR = path.resolve(__dirname, "..", "..", "backend", "data", "producer");
// Broader mount: the entire backend/data/ tree. Used by talking-head where
// inputs span both producer/ (Veo MP4s) and yas_producer/ (image cards) AND
// producer_assets/ (when the user provides intro/outro reference assets that
// land outside producer/).
const BACKEND_DATA_DIR = path.resolve(__dirname, "..", "..", "backend", "data");

console.log(`[remotion] producer data dir → ${DATA_DIR}`);
console.log(`[remotion] backend data dir  → ${BACKEND_DATA_DIR}`);
console.log(`[remotion] starting on :${PORT}`);

// ---------- Bundle once, reuse for every render ----------------------------

const bundlePromise = bundle({
  entryPoint: path.resolve(__dirname, "src", "Root.tsx"),
  // staticFile() assets (brand fonts, logo, bead sprites). The same folder the
  // CLI uses when run from services/remotion, so studio/CLI renders match.
  publicDir: path.resolve(__dirname, "public"),
  onProgress: (p) => {
    if (p === 100 || p % 20 === 0) console.log(`[remotion] bundle: ${p}%`);
  },
})
  .then((bundleLocation) => {
    console.log(`[remotion] bundled → ${bundleLocation}`);
    return bundleLocation;
  })
  .catch((e) => {
    console.error("[remotion] bundle failed:", e);
    throw e;
  });

// ---------- In-memory job store --------------------------------------------

type JobStatus = "queued" | "rendering" | "done" | "failed";

interface Job {
  id: string;
  status: JobStatus;
  progress: number;
  output_path: string;
  template: string;
  error?: string;
  started_at: number;
  finished_at?: number;
  /** Set only for png-sequence jobs (absent = the original h264 export). */
  format?: "png-sequence";
  /** png-sequence results, read back from the files actually on disk. */
  frame_count?: number;
  /** printf-style file name relative to output_path, e.g. "element-%03d.png". */
  frame_pattern?: string;
  /** Index of the first frame file (ffmpeg's -start_number). */
  start_number?: number;
  /**
   * What the composition's calculateMetadata reported about its text
   * (brand-overlay-42: shown / shrunk / partial / dropped, and why), read off
   * the resolved props. Absent when the composition reports none.
   */
  text_fit?: unknown;
}

const jobs = new Map<string, Job>();

// ---------- Render gate ----------------------------------------------------
// Every render (a full /render export AND a single-frame /still) launches
// headless Chrome plus the Rust compositor. `runRender` is deliberately clamped
// to the core count because the target VPS may have one core, so letting an
// unqueued still start alongside an in-flight export would blow past that
// budget and can OOM-kill the export. One at a time, globally.
let renderGate: Promise<unknown> = Promise.resolve();

function withRenderGate<T>(fn: () => Promise<T>): Promise<T> {
  const run = renderGate.then(fn, fn);
  // Keep the chain alive regardless of outcome; the caller sees the rejection.
  renderGate = run.then(
    () => undefined,
    () => undefined,
  );
  return run;
}

// ---------- Express --------------------------------------------------------

const app = express();
app.use(
  cors({
    origin: [
      "http://127.0.0.1:8000",
      "http://localhost:8000",
      "http://127.0.0.1:4242",
      "http://localhost:4242",
    ],
  }),
);
app.use(express.json({ limit: "2mb" }));

// The renderer's own page is served from an ephemeral bundle origin, not from
// this server, so every asset a composition pulls from /files or /data is
// cross-origin to it. Most of those are <img>/<video>, which browsers fetch
// without CORS — but a CSS mask-image is fetched in CORS mode and is dropped
// SILENTLY when the response has no Access-Control-Allow-Origin. A dropped
// mask does not fail: the element simply renders unmasked, so a tinted brand
// graphic comes out as a flat coloured rectangle over the footage.
//
// These two mounts are unauthenticated local files already; a permissive
// header on them adds no access that guessing the port did not.
const allowAssetOrigins: express.RequestHandler = (_req, res, next) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Cross-Origin-Resource-Policy", "cross-origin");
  next();
};

// Serve clips from the shared filesystem so Remotion's renderer can fetch
// them as HTTP URLs (file:// is unreliable inside Chrome Headless).
app.use("/files", allowAssetOrigins, express.static(DATA_DIR));
// Broader mount for talking-head: anything under backend/data/.
// Used by the talking-head composition which pulls images from
// backend/data/yas_producer/ AND the Veo MP4 from backend/data/producer/ AND
// occasionally producer_assets files from backend/data/assets_library/.
app.use("/data", allowAssetOrigins, express.static(BACKEND_DATA_DIR));

app.get("/health", (_req, res) => {
  res.json({ ok: true, jobs: jobs.size });
});

app.post("/render", async (req, res) => {
  const { template, output_path, props, format } = req.body ?? {};
  if (typeof template !== "string" || !template) {
    return res.status(400).json({ error: "template is required" });
  }
  if (typeof output_path !== "string" || !output_path) {
    return res.status(400).json({ error: "output_path is required" });
  }
  if (!props || typeof props !== "object") {
    return res.status(400).json({ error: "props object is required" });
  }
  if (format !== undefined && format !== "png-sequence") {
    return res.status(400).json({ error: `unknown format ${JSON.stringify(format)} (only "png-sequence")` });
  }
  if (format === "png-sequence") {
    // The frames land in a directory named over HTTP: same guard as /still,
    // never write outside the shared data tree.
    const resolved = path.resolve(output_path);
    if (!resolved.startsWith(BACKEND_DATA_DIR + path.sep)) {
      return res.status(400).json({ error: "output_path must be a directory under backend/data" });
    }
  }
  const id = nanoid();
  const job: Job = {
    id,
    status: "queued",
    progress: 0,
    output_path,
    template,
    started_at: Date.now(),
    ...(format === "png-sequence" ? { format: "png-sequence" as const } : {}),
  };
  jobs.set(id, job);
  console.log(`[remotion] queued job=${id} template=${template} → ${output_path}`);
  // Fire-and-forget the render. Errors are captured into the job row.
  void runRender(job, props).catch((e) => {
    job.status = "failed";
    job.error = String((e as Error)?.message ?? e);
    job.finished_at = Date.now();
    console.error(`[remotion] job=${id} failed:`, e);
  });
  res.json({ job_id: id });
});

app.get("/jobs/:id", (req, res) => {
  const job = jobs.get(req.params.id);
  if (!job) return res.status(404).json({ error: "job not found" });
  res.json(job);
});

/**
 * POST /still { template, output_path, props, frame } → { ok, output_path }
 *
 * Renders ONE frame of a composition, synchronously. This is what lets the AI
 * editor look at its own work: it edits the timeline, renders the frame it just
 * changed, and sees the actual composite (overlays on top of footage) instead of
 * guessing from JSON. Single frames are fast enough to sit inside a request.
 */
app.post("/still", async (req, res) => {
  const { template, output_path, props, frame } = req.body ?? {};
  if (typeof template !== "string" || !template) {
    return res.status(400).json({ error: "template is required" });
  }
  if (typeof output_path !== "string" || !output_path) {
    return res.status(400).json({ error: "output_path is required" });
  }
  if (!props || typeof props !== "object") {
    return res.status(400).json({ error: "props object is required" });
  }
  // Stills must never write outside the shared data tree — output_path comes
  // over HTTP, so treat it as untrusted even though only the backend calls us.
  const resolved = path.resolve(output_path);
  if (resolved !== BACKEND_DATA_DIR && !resolved.startsWith(BACKEND_DATA_DIR + path.sep)) {
    return res.status(400).json({ error: "output_path must live under backend/data" });
  }
  try {
    const target = await withRenderGate(async () => {
      const bundleLocation = await bundlePromise;
      const composition = await selectComposition({
        serveUrl: bundleLocation,
        id: template,
        inputProps: props,
      });
      // Clamp into the composition so an out-of-range playhead still renders.
      const wanted = Number.isFinite(Number(frame)) ? Math.floor(Number(frame)) : 0;
      const at = Math.max(0, Math.min(wanted, composition.durationInFrames - 1));
      fs.mkdirSync(path.dirname(resolved), { recursive: true });
      await renderStill({
        composition,
        serveUrl: bundleLocation,
        output: resolved,
        inputProps: props,
        frame: at,
        imageFormat: "jpeg",
        jpegQuality: 80,
      });
      return { at, fit: textFitOf(composition.props) };
    });
    console.log(`[remotion] still template=${template} frame=${target.at} → ${resolved}`);
    res.json({
      ok: true,
      output_path: resolved,
      frame: target.at,
      ...(target.fit !== undefined ? { text_fit: target.fit } : {}),
    });
  } catch (e) {
    const message = String((e as Error)?.message ?? e);
    console.error("[remotion] still failed:", e);
    res.status(500).json({ error: message });
  }
});

// Bind host is configurable so the container can listen on 0.0.0.0 (HOST env)
// for the backend container to reach it; defaults to loopback for local dev.
const HOST = process.env.HOST || "127.0.0.1";
app.listen(PORT, HOST, () => {
  console.log(`[remotion] listening on http://${HOST}:${PORT}`);
});

// ---------- Render driver --------------------------------------------------

async function runRender(job: Job, props: Record<string, unknown>): Promise<void> {
  // Share the gate with /still so a preview can never start Chrome alongside an
  // in-flight export (see withRenderGate).
  if (job.format === "png-sequence") {
    return withRenderGate(() => runFramesInner(job, props));
  }
  return withRenderGate(() => runRenderInner(job, props));
}

/**
 * format "png-sequence": every frame as a transparent PNG in the directory
 * `job.output_path`, for compositions that draw ONLY graphics (e.g.
 * brand-overlay-42). The backend overlays them on the untouched footage with
 * ffmpeg, so the footage itself never passes through Chrome.
 *
 * PNG makes the renderer omit the page background, so undrawn pixels are
 * alpha 0. Chrome, however, writes a frame that has NO transparent pixel (a
 * full-frame wipe) as an RGB PNG, and a sequence that mixes RGB and RGBA files
 * makes ffmpeg re-initialise its filter graph mid-stream: overlaying such a
 * 60-frame sequence produced 55 frames (measured). Every opaque frame is
 * therefore rewritten as RGBA before the job reports done.
 *
 * On success the job reports what is actually on disk: the printf pattern
 * ffmpeg needs (`-start_number N -i dir/pattern`), the first index and the
 * frame count. A count that disagrees with the composition fails the job.
 */
async function runFramesInner(job: Job, props: Record<string, unknown>): Promise<void> {
  job.status = "rendering";
  const bundleLocation = await bundlePromise;
  const outDir = path.resolve(job.output_path);

  const composition = await selectComposition({
    serveUrl: bundleLocation,
    id: job.template,
    inputProps: props,
  });

  fs.mkdirSync(outDir, { recursive: true });
  const total = composition.durationInFrames;
  console.log(
    `[remotion] job=${job.id} rendering ${total} PNG frames`,
    `(${composition.width}x${composition.height} @ ${composition.fps}fps) → ${outDir}`,
  );

  const result = await renderFrames({
    composition,
    serveUrl: bundleLocation,
    inputProps: props,
    outputDir: outDir,
    imageFormat: "png",
    timeoutInMilliseconds: 120000,
    onStart: () => undefined,
    onFrameUpdate: (rendered) => {
      job.progress = total > 0 ? rendered / total : 0;
    },
    // Same clamp as the h264 path (see runRenderInner).
    concurrency: Math.max(1, Math.min(2, os.cpus().length)),
  });

  const found = scanFrameFiles(outDir, path.basename(result.assetsInfo.imageSequenceName));
  let promoted = 0;
  for (const file of found.files) {
    if (promotePngToRgba(file)) promoted++;
  }
  if (promoted) console.log(`[remotion] job=${job.id} rewrote ${promoted} opaque RGB frame(s) as RGBA`);
  if (found.count !== total || result.frameCount !== total) {
    throw new Error(
      `rendered ${result.frameCount} of ${total} frames, but ${outDir} holds ` +
        `${found.count} contiguous ${found.pattern} files from ${found.start}` +
        (found.stray ? ` (+${found.stray} outside that run)` : ""),
    );
  }

  job.frame_count = found.count;
  job.frame_pattern = found.pattern;
  job.start_number = found.start;
  const fit = textFitOf(composition.props);
  if (fit !== undefined) job.text_fit = fit;
  job.status = "done";
  job.progress = 1;
  job.finished_at = Date.now();
  console.log(`[remotion] job=${job.id} done → ${outDir} (${found.pattern} ×${found.count})`);
}

/** A composition's own `text_fit` report on its resolved props (brand-overlay-42). */
function textFitOf(resolved: Record<string, unknown> | undefined): unknown {
  return resolved && "text_fit" in resolved ? resolved.text_fit : undefined;
}

/**
 * Reads the frame files back from `dir`. `printfName` is the renderer's own
 * name for the sequence (e.g. "element-%03d.png"); only files of exactly that
 * shape count, and the run must be contiguous from the lowest index. Anything
 * else of that shape (a stale longer render in a reused dir) is `stray`.
 */
function scanFrameFiles(
  dir: string,
  printfName: string,
): { pattern: string; start: number; count: number; stray: number; files: string[] } {
  const m = /^(.*)%0(\d+)d(\.[A-Za-z0-9]+)$/.exec(printfName);
  if (!m) throw new Error(`unexpected frame file pattern ${printfName}`);
  const [, prefix, pad, ext] = m;
  const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`^${esc(prefix)}(\\d{${Number(pad)}})${esc(ext)}$`);
  const found = fs
    .readdirSync(dir)
    .map((name) => re.exec(name))
    .filter((x): x is RegExpExecArray => x !== null)
    .map((x) => ({ index: Number(x[1]), file: path.join(dir, x[0]) }))
    .sort((a, b) => a.index - b.index);
  const start = found.length ? found[0].index : 0;
  let count = 0;
  while (count < found.length && found[count].index === start + count) count++;
  return {
    pattern: printfName,
    start,
    count,
    stray: found.length - count,
    files: found.slice(0, count).map((f) => f.file),
  };
}

// ---------- PNG: opaque RGB -> RGBA -------------------------------------------

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function pngChunk(type: string, data: Buffer): Buffer {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(data.length, 0);
  head.write(type, 4, "latin1");
  let crc = 0xffffffff;
  for (const byte of head.subarray(4)) crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  for (const byte of data) crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  const tail = Buffer.alloc(4);
  tail.writeUInt32BE((crc ^ 0xffffffff) >>> 0, 0);
  return Buffer.concat([head, data, tail]);
}

/**
 * Rewrites an 8-bit, non-interlaced RGB PNG in place as RGBA with alpha 255
 * (pixels unchanged). Returns false when the file already has an alpha
 * channel. Only the chunks Chrome writes (IHDR, IDAT, IEND) are carried over.
 */
function promotePngToRgba(file: string): boolean {
  const png = fs.readFileSync(file);
  const colorType = png[25];
  if (colorType === 6) return false;
  if (colorType !== 2 || png[24] !== 8 || png[28] !== 0) {
    throw new Error(`${file}: unexpected PNG (bit depth ${png[24]}, colour type ${colorType}, interlace ${png[28]})`);
  }
  const width = png.readUInt32BE(16);
  const height = png.readUInt32BE(20);
  const idat: Buffer[] = [];
  for (let off = 8; off < png.length; ) {
    const len = png.readUInt32BE(off);
    if (png.toString("latin1", off + 4, off + 8) === "IDAT") idat.push(png.subarray(off + 8, off + 8 + len));
    off += 12 + len;
  }
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const stride = width * 3;
  const out = Buffer.alloc((width * 4 + 1) * height); // filter byte 0 (None) per row
  let prev = Buffer.alloc(stride);
  let cur = Buffer.alloc(stride);
  for (let y = 0; y < height; y++) {
    const at = y * (stride + 1);
    const filter = raw[at];
    for (let i = 0; i < stride; i++) {
      const a = i >= 3 ? cur[i - 3] : 0;
      const b = prev[i];
      const c = i >= 3 ? prev[i - 3] : 0;
      let v = raw[at + 1 + i];
      if (filter === 1) v += a;
      else if (filter === 2) v += b;
      else if (filter === 3) v += (a + b) >> 1;
      else if (filter === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a);
        const pb = Math.abs(p - b);
        const pc = Math.abs(p - c);
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      } else if (filter !== 0) throw new Error(`${file}: bad PNG filter ${filter} on row ${y}`);
      cur[i] = v & 0xff;
    }
    const o = y * (width * 4 + 1) + 1;
    for (let x = 0; x < width; x++) {
      out[o + x * 4] = cur[x * 3];
      out[o + x * 4 + 1] = cur[x * 3 + 1];
      out[o + x * 4 + 2] = cur[x * 3 + 2];
      out[o + x * 4 + 3] = 255;
    }
    [prev, cur] = [cur, prev];
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  const tmp = `${file}.rgba.tmp`;
  fs.writeFileSync(
    tmp,
    Buffer.concat([
      png.subarray(0, 8),
      pngChunk("IHDR", ihdr),
      pngChunk("IDAT", zlib.deflateSync(out)),
      pngChunk("IEND", Buffer.alloc(0)),
    ]),
  );
  fs.renameSync(tmp, file);
  return true;
}

async function runRenderInner(job: Job, props: Record<string, unknown>): Promise<void> {
  job.status = "rendering";
  const bundleLocation = await bundlePromise;

  const composition = await selectComposition({
    serveUrl: bundleLocation,
    id: job.template,
    inputProps: props,
  });

  // Ensure parent dir exists on the shared filesystem.
  fs.mkdirSync(path.dirname(job.output_path), { recursive: true });

  console.log(
    `[remotion] job=${job.id} rendering ${composition.durationInFrames} frames`,
    `(${composition.width}x${composition.height} @ ${composition.fps}fps)`,
  );

  await renderMedia({
    composition,
    serveUrl: bundleLocation,
    codec: "h264",
    outputLocation: job.output_path,
    inputProps: props,
    onProgress: ({ progress }) => {
      job.progress = progress;
    },
    // Limit concurrency to keep memory reasonable on a dev laptop, but never
    // exceed the number of CPU cores — Remotion rejects concurrency > cores
    // (a 1-core VPS would otherwise fail every render with
    // "Maximum for --concurrency is 1 (number of cores on this system)").
    concurrency: Math.max(1, Math.min(2, os.cpus().length)),
  });

  job.status = "done";
  job.progress = 1;
  job.finished_at = Date.now();
  console.log(`[remotion] job=${job.id} done → ${job.output_path}`);
}
