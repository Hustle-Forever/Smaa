"""The icon parity report: one page that puts what the video stamps next to
what the studio's canvas draws, for the circle and the chip — free, local.

  python -m app.services.rebrand_harness.icon_parity [--out FILE] [--no-bundle]

At rest the studio shows each icon's exact sprite (the run's own Stamper
pixels). Only for the moment between an edit and its plan (~200 ms) does the
canvas draw the icon itself (iconArt.drawBody: the same bold path, geometry
and label font, a canvas shadow for the Gaussian one), and while a chip grows
it draws the pill unfolding (iconArt.drawChipGrown). This page measures how
close those drawings are:

  cases      code, piggy-bank, clock and users-three x circle / chip x S / M /
             L on a 720 x 1280 frame, plus an Arabic chip: the server's sprite
             (Stamper.sprite_of — the bytes the studio serves) beside the
             canvas's drawing of the same icon, and their difference
  numbers    body IoU (alpha >= 128), mean and 95th-percentile |delta| (0-255,
             over mid-grey) on the whole sprite; pass = IoU >= 0.995, mean <=
             1.5, p95 <= 4 (icon_look §2.6-5)
  checks     made here, in Python: the sprite PNG decodes to the Stamper's
             bytes, and the run's stamped frame at the settle (Stamper.under)
             is the sprite pasted where it rests (the exact still == the run
             frame)
  grow       a chip's grow, frame by frame, in and out: the Stamper's frames
             beside the canvas's (drawItem with the grow drawing) — the
             numbers are the same (rebrandIconFx.ts, fixture-checked), the
             pixels the canvas's own

The page runs the studio's own drawing code: frontend/src/components/yas/
rebrand-studio/iconArt.ts bundled by the frontend's esbuild (node). The
numbers are worked out in the page, in the browser that opens it (they
depend on its canvas), and printed at its top (#verdict). No model is ever
called; nothing is written but the page.
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from . import icon_catalog as ic
from . import icon_render as ir
from . import label_font
from . import recipe as rc
from .elements import icon as icon_el

REPO = Path(__file__).resolve().parents[4]
FRONTEND = REPO / "frontend"
ICON_ART = FRONTEND / "src" / "components" / "yas" / "rebrand-studio" / "iconArt.ts"
W, H = 720, 1280
FILL, INK = "#7f2bf2", "#ffffff"
GLYPHS = (("code", "Developer"), ("piggy-bank", "Tuition-Free"), ("clock", "Deadline"),
          ("users-three", "Peer Learning"))
SIZES = (("S", 0.12), ("M", 0.16), ("L", 0.20))
ARABIC = ("code", "مطور")
GROW = ("piggy-bank", "Tuition-Free", 0.16)
GROW_SPAN_F = 45                             # the grown chip's frames on screen
THRESHOLDS = {"iou": 0.995, "mean": 1.5, "p95": 4.0}


@dataclass
class Case:
    id: str
    glyph: str
    style: str
    size: str
    label: str
    resolved: dict
    sprite: ir.Sprite
    body: tuple[int, int, int, int]          # the Stamper's body box, px
    checks: dict


def resolved_item(glyph: str, style: str, size: float, label: str, *, entrance: str = "none",
                  span_f: int = 30) -> dict:
    """One icon as the planner freezes it (IconResolved) — the circle a
    square at `size` of the short side, the chip 0.74 of that tall and as
    wide as its words (chip_geometry) — resting near the top-left."""
    side = size * min(W, H)
    font = label_font.default_key()
    if style == "chip":
        h = icon_el.chip_height(side)
        geo = icon_el.chip_geometry(label, h, W, font)
        box = {"x": 0.05, "y": 0.1, "w": round(geo.w / W, 6), "h": round(h / H, 6)}
        extra = {"text": geo.text, "font": font}
    else:
        box = {"x": 0.05, "y": 0.1, "w": round(side / W, 6), "h": round(side / H, 6)}
        extra = {}
    return {"glyph": {"source": "library", "id": glyph}, "label": label, "from_sec": 0.0,
            "settle_sec": round(icon_el.IN_F / icon_el.FPS, 4), "to_sec": round(span_f / icon_el.FPS, 4),
            "box": box, "fill": FILL, "ink": INK, "fx": {"entrance": entrance, "seed": f"parity:{glyph}"},
            "style": style, "weight": "bold", "look_v": ir.LOOK_V, **extra}


def _png(rgba: np.ndarray) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgba), "RGBA").save(buf, format="PNG")
    return buf.getvalue()


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def _checks(r: dict, stamps: ir.Stamper) -> dict:
    """The Python-side checks of one case: the sprite PNG (what the studio
    serves) decodes to the Stamper's own bytes; the run's frame at the
    settle is that sprite pasted where it rests."""
    from PIL import Image

    sp = stamps.sprite_of(0)
    png = np.asarray(Image.open(io.BytesIO(_png(sp.rgba))))
    frame = np.full((H, W, 3), 128, np.uint8)
    stamped = stamps.under(frame, float(r["settle_sec"]) + 0.2)
    pasted = frame.copy()
    x, y = stamps.items[0].box[0] - sp.pad, stamps.items[0].box[1] - sp.pad
    ir.composite(pasted, sp.rgba, x, y)
    return {"png_is_sprite": bool(png.dtype == np.uint8 and np.array_equal(png, sp.rgba)),
            "still_is_run_frame": bool(np.array_equal(stamped, pasted))}


def cases() -> list[Case]:
    """Every case the page shows (module docstring), with its sprite."""
    out: list[Case] = []
    rows = [(g, style, s, v, label) for g, label in GLYPHS for style in ("circle", "chip") for s, v in SIZES]
    rows.append((ARABIC[0], "chip", "M", 0.16, ARABIC[1]))
    for glyph, style, s, v, label in rows:
        r = rc.IconResolved.model_validate(resolved_item(glyph, style, v, label)).model_dump(mode="json")
        stamps = ir.Stamper([r], fps=icon_el.FPS, frame_size=(W, H))
        sp = stamps.sprite_of(0)
        tag = "ar" if label == ARABIC[1] else s
        out.append(Case(id=f"{glyph}-{style}-{tag}", glyph=glyph, style=style, size=tag, label=label,
                        resolved=r, sprite=sp, body=stamps.items[0].box, checks=_checks(r, stamps)))
    return out


def grow_frames() -> dict:
    """A chip's grow in and out (GROW), as the Stamper draws each frame:
    {resolved, sprite {url, pad, w, h}, frames: [{f, url, dx, dy, unfold,
    words, scale, alpha}]}."""
    glyph, label, size = GROW
    r = rc.IconResolved.model_validate(
        resolved_item(glyph, "chip", size, label, entrance="grow", span_f=GROW_SPAN_F)).model_dump(mode="json")
    stamps = ir.Stamper([r], fps=icon_el.FPS, frame_size=(W, H))
    sp = stamps.sprite_of(0)
    fin = stamps.items[0].f_settle - stamps.items[0].f_from
    frames = []
    for f in [*range(0, fin + 1), *range(GROW_SPAN_F - ir.OUT_FRAMES - 1, GROW_SPAN_F)]:
        got = stamps.frame_of(0, f / icon_el.FPS)
        pr = ir.fx_progress("grow", f, end=GROW_SPAN_F, fin=fin, fout=ir.OUT_FRAMES)
        if got is None or pr is None:
            continue
        frames.append({"f": f, "url": _data_url(_png(got.rgba), "image/png"), "dx": int(got.dx), "dy": int(got.dy),
                       "unfold": round(pr.unfold, 4), "words": round(pr.words, 4), "scale": round(pr.scale, 4),
                       "alpha": round(pr.alpha, 4), "phase": pr.phase})
    return {"resolved": r, "sprite": {"url": _data_url(_png(sp.rgba), "image/png"), "pad": sp.pad, "w": sp.w,
                                      "h": sp.h, "cw": int(sp.rgba.shape[1]), "ch": int(sp.rgba.shape[0])},
            "frames": frames}


def fonts() -> dict:
    """The chip label font as the studio loads it (loadLabelFont), its faces
    inlined."""
    m = label_font.manifest()
    key = m["default"]
    lab = m["labels"][key]
    return {"default": key, "labels": {key: {
        "latin": _data_url(label_font.face_file(lab["latin"]).read_bytes(), "font/ttf"),
        "arabic": _data_url(label_font.face_file(lab["arabic"]).read_bytes(), "font/ttf")}}}


def bundle(out_dir: Path) -> str:
    """The studio's drawing code (iconArt.ts and what it imports) as one
    browser script, by the frontend's own esbuild; exposes window.parity."""
    node = shutil.which("node")
    esbuild = FRONTEND / "node_modules" / "esbuild" / "bin" / "esbuild"
    if node is None or not esbuild.is_file():
        raise RuntimeError("the page needs node and the frontend's esbuild (npm install in frontend/)")
    entry = out_dir / "parity_entry.ts"
    art = ICON_ART.as_posix()
    entry.write_text(
        f'import {{ drawBody, drawChipGrown, drawItem, isRtl, loadLabelFont, rememberPath }} from "{art}";\n'
        'import { motionFor } from "' + (FRONTEND / "src" / "lib" / "rebrandIconFx.ts").as_posix() + '";\n'
        "(window as any).parity = { drawBody, drawChipGrown, drawItem, isRtl, loadLabelFont, rememberPath, "
        "motionFor };\n", encoding="utf-8")
    js = out_dir / "parity_bundle.js"
    run = subprocess.run([node, str(esbuild), str(entry), "--bundle", "--format=iife", "--platform=browser",
                          "--target=es2020", "--log-level=error", f"--outfile={js}"],
                         capture_output=True, text=True, timeout=120, cwd=str(FRONTEND))
    if run.returncode != 0:
        raise RuntimeError(f"esbuild failed: {(run.stderr or run.stdout)[:400]}")
    return js.read_text(encoding="utf-8")


def page(cs: Sequence[Case], grow: dict, *, script: Optional[str]) -> str:
    """The report page (module docstring)."""
    data = {
        "frame": {"w": W, "h": H}, "thresholds": THRESHOLDS, "rules": icon_el.rules(), "fonts": fonts(),
        "paths": {g: ic.library_d(g, "bold") for g in sorted({c.glyph for c in cs} | {GROW[0]})},
        "cases": [{"id": c.id, "glyph": c.glyph, "style": c.style, "size": c.size, "label": c.label,
                   "text": c.resolved.get("text"), "url": _data_url(_png(c.sprite.rgba), "image/png"),
                   "pad": c.sprite.pad, "w": c.sprite.w, "h": c.sprite.h, "cw": int(c.sprite.rgba.shape[1]),
                   "ch": int(c.sprite.rgba.shape[0]), "checks": c.checks} for c in cs],
        "grow": grow,
    }
    checks_ok = all(all(c.checks.values()) for c in cs)
    rows = "\n".join(
        f'<tr><td>{html.escape(c.id)}</td><td>{"yes" if c.checks["png_is_sprite"] else "NO"}</td>'
        f'<td>{"yes" if c.checks["still_is_run_frame"] else "NO"}</td></tr>' for c in cs)
    body = PAGE.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
    body = body.replace("__CHECK_ROWS__", rows)
    body = body.replace("__CHECKS__", "every sprite and still is the run's own" if checks_ok
                        else "a sprite or a still differs from the run's")
    body = body.replace("__SCRIPT__", (script or "window.parity = null;").replace("</script", "<\\/script"))
    return body


def build(out: Path, *, with_bundle: bool = True) -> Path:
    """Write the page to `out` (module docstring). Free; no model call."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cs = cases()
    grow = grow_frames()
    script = None
    if with_bundle:
        with tempfile.TemporaryDirectory() as tmp:
            script = bundle(Path(tmp))
    out.write_text(page(cs, grow, script=script), encoding="utf-8")
    return out


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Icon parity</title>
<style>
:root { --bg:#fbfaff; --fg:#1c1b22; --muted:#6b6878; --line:#e3e0ee; --card:#ffffff; --ok:#1d7a46; --bad:#b3261e;
        --accent:#7f2bf2; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#141319; --fg:#eeecf5; --muted:#a19eb0;
        --line:#2c2a36; --card:#1c1b24; --ok:#5fd394; --bad:#ff8a80; --accent:#b58cff; } }
:root[data-theme="dark"] { --bg:#141319; --fg:#eeecf5; --muted:#a19eb0; --line:#2c2a36; --card:#1c1b24;
        --ok:#5fd394; --bad:#ff8a80; --accent:#b58cff; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:16px; margin:28px 0 8px; }
p { margin:6px 0; color:var(--muted); max-width:70ch; }
#verdict { font-weight:600; color:var(--fg); }
.ok { color:var(--ok); } .bad { color:var(--bad); }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(320px, 1fr)); gap:12px; }
.case { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:10px; overflow:hidden; }
.case h3 { font-size:13px; margin:0 0 6px; display:flex; justify-content:space-between; gap:8px; }
.pics { display:flex; gap:6px; align-items:flex-start; }
.pics figure { margin:0; flex:1 1 0; min-width:0; } .pics figcaption { font-size:11px; color:var(--muted); }
canvas, img { display:block; background:#808080; border-radius:4px; width:100%; height:auto; }
table { border-collapse:collapse; font-size:12px; width:100%; }
td, th { border-bottom:1px solid var(--line); padding:4px 6px; text-align:left; }
.nums { font:12px ui-monospace, monospace; color:var(--muted); }
.scroll { overflow-x:auto; }
</style>
</head>
<body>
<main>
<h1>Icon parity</h1>
<p>What the video stamps (the server's sprite) beside what the studio's canvas draws for the same icon — the drawing
it shows only for the moment between an edit and its plan, and while a chip grows. At rest the studio always shows the
server's sprite itself.</p>
<p id="verdict">Measuring…</p>
<p>Pass: body IoU ≥ <span id="t-iou"></span>, mean |Δ| ≤ <span id="t-mean"></span> and p95 ≤ <span id="t-p95"></span>
(0–255, over mid-grey, the whole sprite). Checked in Python: __CHECKS__.</p>
<h2>At rest</h2>
<div class="grid" id="cases"></div>
<h2>A chip's grow, frame by frame</h2>
<p>Left the video's frame, right the canvas's: the same numbers (checked against a shared table), the canvas's own
pixels.</p>
<div class="grid" id="grow"></div>
<h2>Python checks</h2>
<div class="scroll"><table><thead><tr><th>case</th><th>sprite PNG = Stamper's bytes</th><th>still = run frame</th></tr></thead>
<tbody>__CHECK_ROWS__</tbody></table></div>
</main>
<script>__SCRIPT__</script>
<script>
const DATA = __DATA__;
const P = window.parity;
const T = DATA.thresholds;
document.getElementById("t-iou").textContent = T.iou;
document.getElementById("t-mean").textContent = T.mean;
document.getElementById("t-p95").textContent = T.p95;

function img(url) { return new Promise((ok, bad) => { const i = new Image(); i.onload = () => ok(i); i.onerror = bad; i.src = url; }); }
function canvas(w, h) { const c = document.createElement("canvas"); c.width = w; c.height = h; return c; }

function metrics(a, b, n) {
  let inter = 0, uni = 0, sum = 0, inkSum = 0, inkN = 0;
  const d = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const aa = a[4 * i + 3], ba = b[4 * i + 3];
    const A = aa >= 128, B = ba >= 128;
    if (A && B) inter++;
    if (A || B) uni++;
    let m = 0;
    for (let k = 0; k < 3; k++) {
      const ca = (a[4 * i + k] * aa + 128 * (255 - aa)) / 255;
      const cb = (b[4 * i + k] * ba + 128 * (255 - ba)) / 255;
      m += Math.abs(ca - cb);
    }
    d[i] = m / 3;
    sum += d[i];
    if (aa > 8 || ba > 8) { inkSum += d[i]; inkN++; }
  }
  const sorted = Array.from(d).sort((x, y) => x - y);
  return { iou: uni ? inter / uni : 1, mean: sum / n, p95: sorted[Math.floor(0.95 * (n - 1))],
           meanInk: inkN ? inkSum / inkN : 0 };
}

function diff(a, b, w, h) {
  const c = canvas(w, h);
  const x = c.getContext("2d");
  const out = x.createImageData(w, h);
  for (let i = 0; i < w * h; i++) {
    let m = 0;
    for (let k = 0; k < 3; k++) m = Math.max(m, Math.abs(a[4 * i + k] * a[4 * i + 3] - b[4 * i + k] * b[4 * i + 3]) / 255);
    m = Math.max(m, Math.abs(a[4 * i + 3] - b[4 * i + 3]));
    const v = Math.min(255, m * 8);
    out.data[4 * i] = v; out.data[4 * i + 1] = v; out.data[4 * i + 2] = v; out.data[4 * i + 3] = 255;
  }
  x.putImageData(out, 0, 0);
  return c;
}

function fig(el, caption) {
  const f = document.createElement("figure");
  f.appendChild(el);
  const c = document.createElement("figcaption");
  c.textContent = caption;
  f.appendChild(c);
  return f;
}

async function fontsReady() {
  P.loadLabelFont(DATA.fonts);
  const until = Date.now() + 5000;
  while (Date.now() < until) {
    if (document.fonts.check('600 20px "42 Label"') && document.fonts.check('600 20px "42 Label"', "مطور")) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return false;
}

async function run() {
  const verdict = document.getElementById("verdict");
  if (!P) { verdict.textContent = "The studio's drawing code isn't in this page (built with --no-bundle)."; return; }
  for (const [id, d] of Object.entries(DATA.paths)) if (d) P.rememberPath(id, "bold", d);
  const fontOk = await fontsReady();
  let pass = 0;
  const results = [];
  for (const c of DATA.cases) {
    const sprite = await img(c.url);
    const a = canvas(c.cw, c.ch);
    a.getContext("2d").drawImage(sprite, 0, 0);
    const b = canvas(c.cw, c.ch);
    const ctx = b.getContext("2d");
    P.drawBody(ctx, { source: "library", id: c.glyph },
      { style: c.style, fill: "#7f2bf2", ink: "#ffffff", weight: "bold", text: c.text },
      { x: c.pad, y: c.pad, w: c.w, h: c.h }, DATA.rules);
    const da = a.getContext("2d").getImageData(0, 0, c.cw, c.ch).data;
    const db = ctx.getImageData(0, 0, c.cw, c.ch).data;
    const m = metrics(da, db, c.cw * c.ch);
    const ok = m.iou >= T.iou && m.mean <= T.mean && m.p95 <= T.p95;
    if (ok) pass++;
    results.push({ id: c.id, ...m, ok });
    const card = document.createElement("section");
    card.className = "case";
    card.dataset.case = c.id;
    card.dataset.ok = String(ok);
    card.innerHTML = `<h3><span>${c.id}${c.style === "chip" ? ` · “${c.text}”` : ""}</span><span class="${ok ? "ok" : "bad"}">${ok ? "pass" : "differs"}</span></h3>`;
    const pics = document.createElement("div");
    pics.className = "pics";
    pics.append(fig(a, "video"), fig(b, "canvas"), fig(diff(da, db, c.cw, c.ch), "difference ×8"));
    card.appendChild(pics);
    const nums = document.createElement("p");
    nums.className = "nums";
    nums.textContent = `IoU ${m.iou.toFixed(4)} · mean ${m.mean.toFixed(2)} · p95 ${m.p95.toFixed(2)} · on the ink ${m.meanInk.toFixed(2)}`;
    card.appendChild(nums);
    document.getElementById("cases").appendChild(card);
  }
  // The grow.
  const g = DATA.grow;
  const M = 24;
  const gw = g.sprite.cw + 2 * M, gh = g.sprite.ch + 2 * M;
  const sprite = await img(g.sprite.url);
  const r = g.resolved;
  const box = { x: M + g.sprite.pad, y: M + g.sprite.pad, w: g.sprite.w, h: g.sprite.h };
  for (const fr of g.frames) {
    const a = canvas(gw, gh);
    a.getContext("2d").drawImage(await img(fr.url), M + fr.dx, M + fr.dy);
    const b = canvas(gw, gh);
    const ctx = b.getContext("2d");
    const item = {
      from: r.from_sec, settle: r.settle_sec, to: r.to_sec, enter: "grow", leave: null, seed: "parity",
      body: box, pad: g.sprite.pad,
      draw: (x, y) => ctx.drawImage(sprite, x - g.sprite.pad, y - g.sprite.pad),
      grow: { rtl: P.isRtl(r.text), draw: (x, y, u, wd) => P.drawChipGrown(ctx, r.glyph,
        { style: "chip", fill: r.fill, ink: r.ink, weight: "bold", text: r.text },
        { x, y, w: box.w, h: box.h }, DATA.rules, u, wd) },
    };
    P.drawItem(ctx, item, fr.f / DATA.rules.fps, DATA.frame, DATA.rules);
    const wrap = document.createElement("section");
    wrap.className = "case";
    wrap.dataset.frame = fr.f;
    const pair = document.createElement("div");
    pair.className = "pics";
    pair.append(fig(a, `frame ${fr.f} · video`), fig(b, "canvas"));
    wrap.appendChild(pair);
    const nums = document.createElement("p");
    nums.className = "nums";
    nums.textContent = `${fr.phase} · open ${fr.unfold} · words ${fr.words} · size ${fr.scale}`;
    wrap.appendChild(nums);
    document.getElementById("grow").appendChild(wrap);
  }
  verdict.textContent = `${pass} of ${DATA.cases.length} icons pass` + (fontOk ? "" : " (the label font didn't load: chips drawn in a fallback font)");
  verdict.className = pass === DATA.cases.length ? "ok" : "bad";
  verdict.dataset.pass = String(pass);
  verdict.dataset.total = String(DATA.cases.length);
  window.PARITY = results;
}
run().catch((e) => { document.getElementById("verdict").textContent = "The page failed: " + e; });
</script>
</body>
</html>
"""


def main(argv: Optional[Sequence[str]] = None) -> int:
    from ..rebrand_pipeline import REBRAND_ROOT

    ap = argparse.ArgumentParser(prog="icon_parity", description="The icon parity report page (free, local).")
    ap.add_argument("--out", type=Path, default=REBRAND_ROOT / "_parity" / "icon_parity.html")
    ap.add_argument("--no-bundle", action="store_true", help="leave the studio's drawing code out (no node)")
    args = ap.parse_args(argv)
    out = build(args.out, with_bundle=not args.no_bundle)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
