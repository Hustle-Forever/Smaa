"""The harness run of one talking-head scene — Omni edits it, chunk by chunk,
from a recipe.

Per chunk (≤10 s): its first frame → the speaker's mask → the TARGET frame
(compose) → the prompt (prompt) → Omni → the clip fitted back to the chunk's
own audio → the guards. Every step is recorded in the run's manifest
(stages), so a retry can reuse what did not change.

Two or three patterns are still ONE target image per chunk (every pattern
laid in, in draw order) and one plural clause (prompt.patterns_clause); the
entrance veil guard then keeps the patterns' own ink (guards.veil_keep).

For the 42 default this is the approved run, call for call
(tests/test_rebrand_golden.py runs both paths against one log). The old
loop (rebrand_pipeline._omni_edit) still edits the intro/outro cards and
any scene a request sends down path "legacy".

Built on a finished version by "Add on top" (ctx.base in stack mode), a
scene takes _edit_stacked instead: Omni edits the version's own part (not
the original), shown the version's settled first frame with only the NEW
layers laid in, told to keep what is there (prompt.KEEP_CLAUSE) under the
harness's own opening; the guards then keep the version exactly as it is
outside the new layers (the hold) and put the speaker back from the TRUE
original. A part the new layers do not show in is the version's, copied —
no Omni call.

A recipe with an icon element (icons_by_pipeline) has its icons STAMPED by
us, inside the speaker pass (speaker_front.compose_frame: behind the
speaker), from the plan frozen at Generate (props.resolved): every prompt
ends with ICONS_CLAUSE, the pass always runs (pattern or not), and each
part keeps its PRE-STAMP clip (Omni's, fitted: <label>_c<j>_prestamp.mp4)
and its CLEAN one (the pass without our icons), uploaded beside the
version, with how the pass ran — the "icons" stage. "Add on top" of such a
version with only icons changed runs that pass again with the new icons:
no Omni call. A run whose speaker pass fails lays on only the icons the
speaker never reaches (speaker_front.stamp_only).

Omni's own marks outside what it was asked for, on a run that asks it for
nothing but the patterns (guards.asks_omni_for_more — not the approved 42
default, whose brief asks for icons): with the recipe's clean-up on
(guards.stray "clean"), the speaker pass is handed the footprint of what
the recipe draws (footprint: the patterns' layers and their ink in the
target, grown a little, and our icons' boxes) and puts everything outside
it back to the original. Off, the run looks for such marks (stray.find)
and the version says so, with the clean-up ("Change it", reusing Omni's
clip) as the way to be rid of them. A pass on top of a version has no
clean-up of its own: the hold already gives the version back outside the
new layers.

Used when a run carries a recipe (the Rebrand studio), or for every scene
run when REBRAND_HARNESS=scene is set.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .. import omni_client as omni
from .. import person_matte
from .. import rebrand_media as media
from .. import rebrand_pattern_ref as pattern_ref
from .. import rebrand_store as store
from .. import speaker_front
from . import base as hbase
from . import compose
from . import guards as gd
from . import prompt as hprompt
from . import recipe as rc
from . import stages as st
from . import stray
from .elements import icon as icon_el

log = logging.getLogger("archivist.rebrand.harness")


def enabled(options: dict) -> bool:
    """Whether a scene run goes through the harness."""
    if options.get("path") == "legacy":
        return False
    if options.get("recipe"):
        return True
    return os.getenv("REBRAND_HARNESS", "").strip().lower() in ("scene", "1", "on", "true")


def head(recipe: Optional[rc.Recipe], brief: dict) -> str:
    """The prompt's opening: the brief's (the approved flow) or the harness's."""
    if recipe is not None and recipe.director.head == "harness":
        return hprompt.harness_head(recipe.director.intensity, recipe.director.notes)
    return brief.get("omni_scene_prompt") or ""


_ICON_WORDS = re.compile(r"\b(icons?|symbols?|badges?|pictograms?|emoji)\b", re.IGNORECASE)


def asks_for_icons(opening: str) -> Optional[str]:
    """Whether a brief's opening asks Omni for icons of its own (a word for
    them that is not said as a "no" / "never" / "without"): why, or None."""
    from .. import brand_icons

    for m in _ICON_WORDS.finditer(opening or ""):
        if not brand_icons.negated_in_clause(opening, m.start()):
            return f"the brief's opening asks Omni for {m.group(0).lower()}, and ours are stamped after it"
    return None


def run_brief(recipe: Optional[rc.Recipe], brief: dict,
              refs: hprompt.Refs) -> tuple[dict, Optional[str]]:
    """The brief a run opens with, and why its opening gave way (None when
    it did not). A brief opening is written for the pattern it was told
    about ("graphics ONLY in the left half"): with several patterns, one
    that names a place no pattern covers, or confines the graphics to fewer
    sides than they cover, gives way to the harness's own opening for this
    run (the stored brief is kept as written). One pattern: as it is.

    With our icons stamped after Omni (icons_by_pipeline), an opening that
    still asks Omni for icons, symbols or badges gives way the same way:
    two sets would show."""
    if recipe is None or recipe.director.head != "brief":
        return brief, None
    why = None
    if rc.icons_by_pipeline(recipe):
        why = asks_for_icons(brief.get("omni_scene_prompt") or "")
    if why is None and len(hprompt.as_refs(refs)) > 1:
        why = hprompt.head_misplaces(brief.get("omni_scene_prompt") or "", refs)
    if why:
        return {**brief, "omni_scene_prompt": hprompt.harness_head(
            recipe.director.intensity, recipe.director.notes)}, why
    return brief, None


def stacked(ctx: Any) -> bool:
    """Whether the run adds on top of a finished version ("Add on top")."""
    b = getattr(ctx, "base", None)
    return b is not None and getattr(b, "mode", None) == "stack"


def layers(refs: hprompt.Refs, recipe: rc.Recipe) -> list[compose.Layer]:
    """The recipe's layers for the TARGET frame, back to front: one per
    pattern in list order (the first is the back-most). With several, every
    layer clears the bars when any pattern asks, as the restore then does."""
    found = hprompt.as_refs(refs)
    bars = gd.clears_bars(recipe.guards, found)
    out = []
    for i, ref in enumerate(found):
        layer = compose.pattern_layer(ref) if i == 0 else compose.pattern_layer(ref, name=f"pattern-{i + 1}")
        layer.clears_bars = bars
        out.append(layer)
    return out


def target(frame: Path, out: Path, refs: hprompt.Refs, recipe: rc.Recipe,
           *, person=None, alphas: Optional[list] = None) -> Path:
    """One chunk's TARGET frame (synchronous). `alphas`: compose_target's
    read-only capture of each layer's alpha."""
    if alphas is not None:
        return compose.compose_target(frame, out, layers(refs, recipe), person=person, alphas=alphas)
    return compose.compose_target(frame, out, layers(refs, recipe), person=person)


def veil_keep(frame: Path, refs: hprompt.Refs, recipe: rc.Recipe):
    """The veil guard's keep mask for this chunk: the patterns' union alpha
    at the frame's size, grown a little (guards.veil_keep)."""
    from PIL import Image

    with Image.open(frame) as im:
        width, height = im.size
    return compose.union_alpha(layers(refs, recipe), width, height)


def icon_boxes(stamp: Any) -> list[tuple[int, int, int, int]]:
    """Every settled box (pixel x0, y0, x1, y1) of a chunk's Stamper — the
    icons it lays in at any moment — or none."""
    if stamp is None:
        return []
    return [(it.box[0], it.box[1], it.box[0] + it.box[2], it.box[1] + it.box[3]) for it in stamp.items]


def stray_footprint(size: tuple[int, int], refs: hprompt.Refs, recipe: rc.Recipe, *,
                    target: Optional[Path] = None, frame: Optional[Path] = None, stamp: Any = None):
    """The stray-drawing clean-up's footprint for one chunk (float HxW at
    `size`, the clip's (W, H); guards.stray_footprint), synchronous: the
    patterns' layers — whole, not cut by the speaker, who moves — with
    their ink in the TARGET frame against the frame it was drawn on, and
    the boxes of the icons `stamp` lays in. A fresh run's (or a "Change
    it"'s) only: on top of a version the hold keeps it as it is."""
    import numpy as np

    width, height = size
    ink = np.zeros((height, width), dtype=bool)
    if hprompt.as_refs(refs):
        ink |= hbase.new_ink(layers(refs, recipe), width, height) > 0.02
    if target is not None and frame is not None and Path(target).is_file() and Path(frame).is_file():
        ink |= gd.drawn_ink(target, frame, width, height)
    return gd.stray_footprint(ink, boxes=icon_boxes(stamp))


def pattern_refs(ctx: Any) -> list[pattern_ref.PatternRef]:
    """The run's patterns in draw order. `ctx.pattern_ref` is the first one
    (what the single-pattern code and the evidence read); the list is used
    only while it agrees with it."""
    ref = ctx.pattern_ref
    many = list(getattr(ctx, "pattern_refs", None) or [])
    if many and many[0] is ref:
        return many
    return [ref] if ref is not None else []


def _slug(name: str) -> str:
    return name.replace(" ", "_").lower()


async def _thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


async def _step(exp_id: int, arm: str, name: str, status: str = "running", detail: str = "") -> None:
    try:
        await store.append_arm_step(exp_id, arm, {"name": name, "status": status, "detail": detail[:300]})
    except Exception:  # noqa: BLE001 — progress reporting must never kill a run
        log.exception("[rebrand] step report failed")


# ---------------------------------------------------------------------------
# Icons: what a run stamps, and what each part keeps
# ---------------------------------------------------------------------------

@dataclass
class Icons:
    """What a scene's run stamps: the icons (IconResolved dicts, scene
    seconds), each chunk's (start, seconds) in the scene and its Stamper —
    None for a chunk no icon shows in."""
    resolved: list
    spans: list
    stamps: list
    frame_size: tuple[int, int]
    warnings: list = field(default_factory=list)

    def labels(self, j: int) -> list[str]:
        start, sec = self.spans[j]
        return [r.get("label") or r["glyph"]["id"] for r in self.resolved if icon_el.overlaps(r, start, sec)]

    async def unmatted(self, j: int) -> Any:
        """Chunk j's Stamper for stamp_only: only the icons the speaker
        never reaches (no matte to put them behind)."""
        safe = icon_el.unmatted(self.resolved)
        return (await icon_el.stampers(safe, [self.spans[j]], frame_size=self.frame_size))[0]


def _spans(chunks: list[Path]) -> list[tuple[float, float]]:
    """Each chunk's (start, seconds) in the scene, as the studio's plan has
    them (icon_el.chunk_spans over the chunks' rounded lengths)."""
    return icon_el.chunk_spans([round(media.probe_duration(c) or 0.0, 2) for c in chunks])


def _plan_inputs(ctx: Any):
    """How a run plans its icons when the recipe came without a frozen plan
    (an API client): the studio's own inputs for this scene, waited for."""
    async def make():
        from . import studio               # late: the studio imports the runner

        spec = hbase.BaseSpec.of(ctx.options.get("base"))
        return await studio.icon_ctx(ctx.run_id, ctx.options.get("target"), ctx.recipe, spec)

    return make


# A run's own warnings about its icons (the version shows them; a $0 pass
# on top of it places them again).
ICONS_NOT_PLACED = "The icons couldn't be placed, so this version has none"


def _drawable(r: dict) -> bool:
    """A library icon the baked catalogue has (an older draft or an API
    recipe can name one a re-bake dropped); a drawn one is read below."""
    from . import icon_catalog

    g = r.get("glyph") or {}
    return g.get("source") == "custom" or icon_catalog.library_d(str(g.get("id") or "")) is not None


async def _loadable(resolved: list) -> tuple[list, list[str]]:
    """The icons whose glyphs can be drawn (a library icon the catalogue
    still has; a drawn one read from its store), and the labels of any that
    cannot."""
    from . import icon_catalog

    if all(_drawable(r) for r in resolved):
        try:
            await icon_catalog.glyphs_for(resolved)
            return list(resolved), []
        except Exception:  # noqa: BLE001 — one by one, to keep the rest
            pass
    keep, lost = [], []
    for r in resolved:
        try:
            if not _drawable(r):
                raise KeyError(r["glyph"]["id"])
            await icon_catalog.glyphs_for([r])
            keep.append(r)
        except Exception:  # noqa: BLE001 — gone from its store, or the store is down
            lost.append(r.get("label") or r["glyph"]["id"])
    return keep, lost


async def icons_for(ctx: Any, arm: str, label: str, chunks: list[Path], manifest: st.Manifest, *,
                    inherited: Optional[list] = None) -> Optional[Icons]:
    """The icons this scene's run stamps, per chunk — None when the recipe
    has no icons of ours (none, or Omni keeps its own) and none are carried
    over: the run is then exactly the pass it always was.

    The plan is the one frozen at Generate (props.resolved); without one it
    is made here by the studio's code. `inherited`: added on top of a
    version with our icons and no icon element of its own, that version's
    icons, stamped again as they were. Records the "icons" stage's plan."""
    recipe = ctx.recipe
    own = recipe is not None and rc.icons_by_pipeline(recipe)
    if not own and inherited is None:
        return None
    made = None
    warnings: list[str] = []
    if own:
        try:
            resolved, made = await icon_el.plan_for_run(recipe, _plan_inputs(ctx))
        except Exception as e:  # noqa: BLE001 — the version goes on without them
            log.exception("[rebrand] planning the icons failed for #%s", ctx.exp_id)
            await _step(ctx.exp_id, arm, f"{label}: icons not placed", status="failed", detail=str(e)[:200])
            warnings.append(ICONS_NOT_PLACED)
            resolved = []
    else:
        resolved = list(inherited or [])
    resolved, lost = await _loadable(resolved)
    for name in lost:
        await _step(ctx.exp_id, arm, f"{label}: icon '{name}' left out", status="failed",
                    detail="its drawing could not be read")
        warnings.append(f"'{name}' couldn't be drawn, so it was left out")
    spans = await _thread(_spans, chunks)
    stamps = await icon_el.stampers(resolved, spans, frame_size=(ctx.width, ctx.height))
    manifest.record("icons", "plan", st.key(resolved=resolved), resolved=resolved,
                    plan_key=made.key() if made is not None else None, planned=made is not None,
                    inherited=not own)
    return Icons(resolved=resolved, spans=spans, stamps=stamps, frame_size=(ctx.width, ctx.height),
                 warnings=warnings)


async def _keep_files(ctx: Any, name: str, j: int, files: dict) -> None:
    """Upload a part's kept files beside the version (best-effort): a
    redeploy can wipe the disk, and a later icon-only pass needs them. A
    record already in the bucket stays as it is; one file kept under two
    names (a version without our icons: its part is both) goes up once."""
    from .. import asset_storage

    sent: dict[str, tuple[str, str]] = {}
    for kind, rec in files.items():
        if not isinstance(rec, dict) or rec.get("bucket") or not rec.get("path"):
            continue
        path = Path(rec["path"])
        if str(path) in sent:
            rec["bucket"], rec["key"] = sent[str(path)]
            continue
        if not path.is_file():
            continue
        mime = "video/mp4" if path.suffix == ".mp4" else "application/octet-stream"
        bucket, key = await asset_storage.persist_local_file(
            abs_path=path, bucket_key=f"yas/rebrand/{ctx.exp_id}/{name}_c{j}_{kind}{path.suffix}", mime=mime)
        if bucket and key:
            rec["bucket"], rec["key"] = bucket, key
            sent[str(path)] = (bucket, key)


def _save_mask(path: Path, mask: Any) -> Path:
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, np.asarray(mask, dtype=np.float32))
    return path


async def _pass_with_icons(ctx: Any, arm: str, label: str, j: int, chunk: Path, edited: Path,
                           g: gd.ChunkGuards, icons: Icons, manifest: st.Manifest, out_dir: Path, *,
                           veil_keep: Any = None, backdrop: Optional[Path] = None, hold: Any = None,
                           footprint: Any = None, kept: Optional[hbase.Replay] = None, strict: bool = False,
                           record: Optional[dict] = None) -> Path:
    """One part's speaker pass WITH the icons (restore_speaker, stamps behind
    the speaker), its clean twin, and the "icons" record: how the pass ran
    and the files it kept, uploaded beside the version. `footprint`: the
    stray-drawing clean-up's, kept with the rest so a pass on top makes the
    part again the same way.

    `kept`: the pass is the version's own, run again (an icon-only pass on
    top of it) — its files are the version's, handed on. Else `edited` is
    Omni's fitted clip, kept as the part's pre-stamp clip. A failed pass
    raises when `strict` (on top of a version: keeping it is the point),
    else lays on only the icons the speaker never reaches, over Omni's clip.
    `record`: more for the part's guards record ({"hold": True} …)."""
    name = _slug(label)
    item = f"c{j}"
    stamp = icons.stamps[j]
    out_dir.mkdir(parents=True, exist_ok=True)
    if kept is not None:
        files = {k: dict(v) for k, v in kept.files.items()}
    else:
        prestamp = out_dir / f"{name}_c{j}_prestamp.mp4"
        await _thread(shutil.copyfile, edited, prestamp)
        files = {"prestamp": {"path": str(prestamp)}}
        if veil_keep is not None:
            files["veil_keep"] = {"path": str(await _thread(_save_mask, out_dir / f"{name}_c{j}_veilkeep.npy",
                                                            veil_keep))}
        if backdrop is not None:
            files["backdrop"] = {"path": str(backdrop)}
        if hold is not None:
            files["hold"] = {"path": str(await _thread(_save_mask, out_dir / f"{name}_c{j}_hold.npy", hold))}
        if footprint is not None:
            files["footprint"] = {"path": str(await _thread(_save_mask, out_dir / f"{name}_c{j}_footprint.npy",
                                                            footprint))}
    extra: dict[str, Any] = {k: v for k, v in (("veil_keep", veil_keep), ("backdrop", backdrop),
                                               ("hold", hold), ("footprint", footprint)) if v is not None}
    # The pass without our icons, once (the version's own pass run again
    # keeps the version's).
    clean = out_dir / f"{name}_c{j}_clean.mp4" if stamp is not None and kept is None else None
    unmatted = False
    try:
        clip = await _thread(gd.apply, edited, chunk, out_dir / f"{name}_c{j}_front.mp4", g,
                             stamps=stamp, clean=clean, cuts=await _thread(media.cuts_of, chunk), **extra)
        if kept is None:
            files["clean"] = {"path": str(clean or clip)}
    except Exception as e:
        log.exception("[rebrand] the speaker pass with icons failed on %s chunk %d", label, j)
        if strict:
            raise
        await _step(ctx.exp_id, arm, f"{label}: speaker not put in front", status="failed",
                    detail=f"Omni's clip kept, with only the icons clear of the speaker ({str(e)[:120]})")
        clip = edited
        safe = await icons.unmatted(j) if stamp is not None else None
        if safe is not None:
            try:
                clip = await _thread(speaker_front.stamp_only, edited, out_dir / f"{name}_c{j}_stamped.mp4", safe)
                unmatted = True
            except Exception:  # noqa: BLE001 — Omni's clip, as the pass always kept it
                log.exception("[rebrand] stamping the icons alone failed on %s chunk %d", label, j)
        if kept is None:
            files["clean"] = {"path": str(edited)}
    manifest.record("guards", item, None, path=Path(clip), **(record or {}), **g.public())
    # Every file the part's pass needs again, in the bucket (a version's own,
    # handed on, is there already; a version without our icons' part is not).
    await _keep_files(ctx, name, j, files)
    rec: dict[str, Any] = {"span": list(icons.spans[j]), "stamped": icons.labels(j),
                           "guards": {"clear_veil_sec": g.clear_veil_sec, "clear_bars": g.clear_bars},
                           "files": files}
    if unmatted:
        rec["unmatted"] = True
    if kept is not None:
        rec["kept_from"] = True
    manifest.record("icons", item, st.key(stamped=[r for r in icons.resolved
                                                   if icon_el.overlaps(r, *icons.spans[j])],
                                          files={k: (v or {}).get("path") for k, v in files.items()}),
                    path=Path(files["prestamp"]["path"]), **rec)
    return clip


async def _stray_check(ctx: Any, arm: str, label: str, j: int, fitted: Path, chunk: Path,
                       footprint: Any, icons: Optional[Icons], chunks: list[Path]) -> Optional[str]:
    """Omni's own marks outside what it was asked for (stray.find), as the
    version's warning — or None. Never fails the run. Any run with the
    clean-up off (guards.checks_strays), our icons or not."""
    stamp = icons.stamps[j] if icons is not None else None
    try:
        t = await _thread(stray.find, fitted, chunk, footprint=footprint,
                          boxes=(lambda at: stamp.boxes(at, layer=None)) if stamp is not None else None)
    except Exception:  # noqa: BLE001 — a check, not a step of the edit
        log.exception("[rebrand] the stray-drawing check failed on %s chunk %d", label, j)
        return None
    if t is None:
        return None
    spans = icons.spans if icons is not None else await _thread(_spans, chunks)
    start = spans[j][0] if j < len(spans) else 0.0
    message = stray.warning(start + t)
    await _step(ctx.exp_id, arm, f"{label}: Omni drew its own symbol", status="failed", detail=message)
    return message


async def edit_scene(ctx: Any, arm: str, seg: Any, out_dir: Path, label: str,
                     manifest: st.Manifest) -> Path:
    """Omni-edit one scene (≤10 s chunks) from ctx.recipe, re-mux each chunk's
    ORIGINAL audio so speech and lip-sync are untouched, and put the speaker
    back in front. Raises on failure. `ctx` is the pipeline's run context."""
    if stacked(ctx):
        return await _edit_stacked(ctx, arm, seg, out_dir, label, manifest)
    recipe: rc.Recipe = ctx.recipe
    refs = pattern_refs(ctx)
    ref = refs[0] if refs else None
    exp_id = ctx.exp_id
    await _step(exp_id, arm, f"{label}: Omni edit")
    src = seg.base or seg.source
    chunks = await _thread(media.split_chunks, src, out_dir / f"chunks_{_slug(label)}")
    opening = head(recipe, ctx.brief)
    enters = hprompt.has_entrance(refs)
    keep_ink = gd.wants_veil_keep(recipe, refs)
    take = str(ctx.options.get("take") or exp_id)
    # Our icons (None: none — the pass exactly as it always was).
    icons = await icons_for(ctx, arm, label, chunks, manifest)
    no_icons = icons is not None
    warnings: list[str] = list(icons.warnings) if icons is not None else []
    # Omni's own marks: taken away by the speaker pass (the clean-up), or
    # else looked for and said.
    clean_up = gd.cleans_strays(recipe)
    check = gd.checks_strays(recipe)
    fitted: list[Path] = []
    for j, chunk in enumerate(chunks):
        item = f"c{j}"
        entering = j == 0 and enters
        text = hprompt.scene_prompt(opening, refs, entrance=entering, no_icons=no_icons)
        images: list[Path] = []
        first: Optional[Path] = None
        if ref is not None:
            first = await _thread(media.extract_frame, chunk,
                                  out_dir / f"{_slug(label)}_c{j}_first.png", at="first")
            # The pattern goes BEHIND the speaker: their mask cuts it out of
            # the target. Without one it is laid over everything, and the
            # prompt alone says where it belongs.
            person = await person_matte.person_mask(first)
            if person is None:
                await _step(exp_id, arm, f"{label}: no speaker mask", status="failed",
                            detail="the pattern is shown over the speaker; the prompt still "
                                   "puts it behind them")
            images = [await _thread(target, first, out_dir / f"{_slug(label)}_c{j}_target.png",
                                    refs, recipe, person=person)]
            manifest.record("target", item, st.key(frame=first, image=images[0]),
                            path=images[0])
        raw = out_dir / f"{_slug(label)}_c{j}_raw.mp4"
        k = st.key(chunk=chunk, prompt=text, images=images, model=omni.OMNI_MODEL,
                   aspect=ctx.aspect, take=take)
        prior = manifest.reusable("omni", item, k)
        t0 = time.monotonic()
        if prior is not None:
            raw.parent.mkdir(parents=True, exist_ok=True)
            if prior.resolve() != raw.resolve():
                await _thread(shutil.copyfile, prior, raw)
            res = {"path": raw}
            await _step(exp_id, arm, f"{label}: chunk {j + 1} reused", status="done",
                        detail="nothing Omni is shown or told changed")
        else:
            res = await omni.edit_video(chunk, text, aspect=ctx.aspect, images=images, out=raw)
        manifest.record("omni", item, k, path=Path(res["path"]), reused=prior is not None,
                        seconds=time.monotonic() - t0, entering=entering)
        clip = await _thread(
            media.fit_video_to_audio, res["path"], chunk,
            out_dir / f"{_slug(label)}_c{j}.mp4",
            width=ctx.width, height=ctx.height,
        )
        if icons is not None:
            # The pass always runs with icons (pattern or not): they go
            # behind the speaker, and only the speaker pass can put them there.
            g = dataclasses.replace(gd.for_chunk(recipe.guards, refs, entering=entering, veil_keep=keep_ink,
                                                 stray_clean=clean_up), speaker_front=True)
            ink = await _thread(veil_keep, first, refs, recipe) if (refs and first is not None) else None
            if check:
                found = await _stray_check(ctx, arm, label, j, clip, chunk, ink, icons, chunks)
                if found:
                    warnings.append(found)
            fp = await _thread(stray_footprint, (ctx.width, ctx.height), refs, recipe,
                               target=images[0] if images else None, frame=first,
                               stamp=icons.stamps[j]) if g.stray_clean else None
            clip = await _pass_with_icons(ctx, arm, label, j, chunk, clip, g, icons, manifest, out_dir,
                                          veil_keep=ink if g.veil_keep else None, footprint=fp)
        elif ref is not None:
            g = gd.for_chunk(recipe.guards, refs, entering=entering, veil_keep=keep_ink,
                             stray_clean=clean_up and recipe.guards.speaker_front)
            ink = None
            if check:
                # Omni asked for the patterns only (the harness's opening),
                # clean-up off: the version says what it drew besides. Never
                # the approved run, whose brief asks for icons.
                ink = await _thread(veil_keep, first, refs, recipe) if first is not None else None
                found = await _stray_check(ctx, arm, label, j, clip, chunk, ink, None, chunks)
                if found:
                    warnings.append(found)
            if g.speaker_front:
                # A failure keeps Omni's clip rather than losing the edit.
                try:
                    extra = {}
                    if g.veil_keep and first is not None:
                        extra["veil_keep"] = ink if ink is not None else await _thread(
                            veil_keep, first, refs, recipe)
                    if g.stray_clean:
                        extra["footprint"] = await _thread(
                            stray_footprint, (ctx.width, ctx.height), refs, recipe,
                            target=images[0] if images else None, frame=first)
                    clip = await _thread(gd.apply, clip, chunk,
                                         out_dir / f"{_slug(label)}_c{j}_front.mp4", g,
                                         cuts=await _thread(media.cuts_of, chunk), **extra)
                except Exception as e:  # noqa: BLE001
                    log.exception("[rebrand] putting the speaker in front failed on %s chunk %d",
                                  label, j)
                    await _step(exp_id, arm, f"{label}: speaker not put in front", status="failed",
                                detail=f"Omni's clip kept as is ({str(e)[:160]})")
            # `path`: this part's final clip, so a later version can be built
            # on it (base.chunk_sources).
            manifest.record("guards", item, None, path=Path(clip), **g.public())
        fitted.append(clip)
    out = fitted[0] if len(fitted) == 1 else await _thread(
        media.concat, fitted, out_dir / f"{_slug(label)}.mp4",
    )
    patch: dict[str, Any] = {"provider": omni._preferred}
    if icons is not None or warnings:
        # Only a run with our icons, or with something to say, says so (the
        # approved run's arm is as it was).
        patch["warnings"] = warnings
    await store.patch_arm(exp_id, arm, patch)
    await _step(exp_id, arm, f"{label}: edited", status="done", detail=f"via {omni._preferred}")
    return out


def stack_guards(recipe: rc.Recipe, refs: hprompt.Refs, *, entering: bool,
                 veil_keep: bool = False) -> gd.ChunkGuards:
    """A pass on top's guards for one part (the studio's preview reports the
    same): the recipe's, with the speaker always put back — the pass that
    holds the version — and the bars always cleared: the speaker is never
    in a letterbox bar, and the version's own graphics there must stay as
    they are (outside the new layers the hold already gives the version's
    pixels back; MODNet's specks would otherwise let the original's black
    through over them). Never the stray-drawing clean-up: the hold is it."""
    return dataclasses.replace(gd.for_chunk(recipe.guards, refs, entering=entering, veil_keep=veil_keep),
                               speaker_front=True, clear_bars=True)


def _size(frame: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(frame) as im:
        return im.size


async def _edit_stacked(ctx: Any, arm: str, seg: Any, out_dir: Path, label: str,
                        manifest: st.Manifest) -> Path:
    """"Add on top": the recipe's NEW layers drawn on a finished version
    (ctx.base, prepared by base.stack_parts), which stays exactly as it is.

    Per part j: Omni edits the version's own part S_j (conformed to the
    original part, so it has the first pass's length and timing), shown
    S_j's settled first frame with only the new layers laid in, and told
    the harness's opening, KEEP_CLAUSE and the new layers' clause (their
    entrance on the first part only) — no brief: the brief describes the
    first look and would ask for it again. The clip is fitted back to the
    ORIGINAL part (its audio, its length), then the guards: the veil judged
    against S_j, the hold (outside the new layers' footprint the frame IS
    S_j), the speaker from the TRUE original — never over a letterbox bar,
    whatever the new layers ask (the speaker is never in one, and MODNet's
    specks there would put the original's black back over the version's
    own graphics in it). They always run here: the hold is what keeps the
    version as it was, so a part they fail on fails the run rather than
    ship Omni's second pass unguarded.

    A part the new layers do not show in is S_j as it is — no Omni call.
    No new layer at all (every one failed to build) fails the run: it would
    only copy the version. Stages: base, target, omni, guards (every record
    of a stack run).

    Icons: the version's parts are its CLEAN ones when it has our icons
    (base.stack_parts), and this pass stamps the recipe's own icons — or,
    with no icon element, the version's again as they were — in the same
    guards pass (every prompt then ends with ICONS_CLAUSE). A part no new
    layer shows in, with icons, is the version's own pass run again on its
    pre-stamp part with them (base.Replay) — no Omni call: an icon-only
    change of a finished version costs nothing but that pass. An icon
    element switched off takes the version's icons away: with nothing new
    drawn, each part is its clean one, as it is."""
    bv = ctx.base
    recipe: rc.Recipe = ctx.recipe
    refs = pattern_refs(ctx)
    exp_id = ctx.exp_id
    name = _slug(label)
    drawing = bool(rc.pattern_elements(recipe))
    own_icons = rc.icons_by_pipeline(recipe)
    # The recipe's icon element decides the icons, on or off (its own, or
    # none: "Turn icons off" takes the version's away — its clean parts, no
    # Omni call). No icon element at all: the version's own are kept.
    icon_el_ = rc.icon_element(recipe, enabled=False)
    unstamp = icon_el_ is not None and not own_icons and hbase.has_icons(bv.exp)
    if (drawing or not (own_icons or unstamp)) and not refs:
        why = getattr(ctx, "pattern_error", None) or "the patterns could not be made"
        raise RuntimeError(f"nothing new could be drawn on version #{bv.version_id}: {why}")
    chunks = list(bv.chunks) or await _thread(media.split_chunks, seg.base or seg.source,
                                              out_dir / f"chunks_{name}")
    if len(bv.sources) != len(chunks) or len(bv.frames) != len(chunks):
        raise RuntimeError(f"version #{bv.version_id}'s parts do not match this clip's")
    if refs:
        await _step(exp_id, arm, f"{label}: Omni edit, added on top of version #{bv.version_id}")
    elif own_icons:
        await _step(exp_id, arm, f"{label}: new icons on version #{bv.version_id}",
                    detail="its own pass again, with them — no Omni call")
    else:
        await _step(exp_id, arm, f"{label}: icons taken off version #{bv.version_id}",
                    detail="its parts without them — no Omni call")
    # Our icons: the recipe's own, else — with no icon element — the
    # version's again (its parts here are its clean ones, so they are never
    # drawn twice). None: neither.
    inherited = hbase.icons_of(bv.exp) if (icon_el_ is None and hbase.has_icons(bv.exp)) else None
    icons = await icons_for(ctx, arm, label, chunks, manifest, inherited=inherited)
    replays = list(bv.replays) or [hbase.replay_of_source(s) for s in bv.sources]
    opening = hprompt.harness_head(recipe.director.intensity, recipe.director.notes)
    enters = hprompt.has_entrance(refs)
    keep_ink = gd.wants_veil_keep(recipe, refs)
    # No stray-drawing clean-up of its own: outside the new layers the hold
    # already gives the version's pixels back (cleaned, when it was), and
    # the version's own strays are its parts — "Change it" cleans those.
    take = str(ctx.options.get("take") or exp_id)
    new = layers(refs, recipe)
    width, height = await _thread(_size, bv.frames[0])
    hold = await _thread(hbase.hold_mask, new, width, height)
    shows = bool(new) and float(hold.min()) < 0.999
    fitted: list[Path] = []
    for j, chunk in enumerate(chunks):
        item = f"c{j}"
        src, frame = bv.sources[j], bv.frames[j]
        entering = j == 0 and enters
        # The part still shows the version's own entrance (the hold keeps
        # it): its still is settled if either enters (base.entering).
        settles = entering or hbase.entering(bv.exp, j)
        manifest.record("base", item, st.key(source=src, frame=frame), path=src, frame=str(frame),
                        version=bv.version_id)
        if not shows and icons is not None and icons.stamps[j] is not None:
            # Nothing new drawn here, but icons: the version's own pass
            # again, on its pre-stamp part, with them.
            rp = replays[j]
            kw = rp.kwargs()
            # Its clean-up too, when it had one: what it took away stays away.
            g = gd.ChunkGuards(speaker_front=True, clear_veil_sec=kw["clear_veil_sec"],
                               clear_bars=kw["clear_bars"], veil_keep=rp.veil_keep is not None,
                               stray_clean=rp.footprint is not None)
            try:
                clip = await _pass_with_icons(ctx, arm, label, j, chunk, rp.edited, g, icons, manifest,
                                              out_dir, veil_keep=kw.get("veil_keep"),
                                              backdrop=kw.get("backdrop"), hold=kw.get("hold"),
                                              footprint=kw.get("footprint"), kept=rp,
                                              strict=True, record={"restamped": True})
            except Exception as e:
                await _step(exp_id, arm, f"{label}: icons not laid on", status="failed",
                            detail=f"part {j + 1}: {str(e)[:160]}")
                raise RuntimeError(f"laying the icons on version #{bv.version_id} failed on part {j + 1}: "
                                   f"{str(e)[:160]}") from e
            manifest.record("omni", item, None, path=rp.edited, reused=True, kept=True, restamped=True,
                            settles=settles)
            await _step(exp_id, arm, f"{label}: part {j + 1} icons laid on", status="done",
                        detail="no Omni call")
            fitted.append(clip)
            continue
        if not shows:
            # Nothing new shows here: the version's own part, as it is.
            kept = out_dir / f"{name}_c{j}_kept.mp4"
            kept.parent.mkdir(parents=True, exist_ok=True)
            await _thread(shutil.copyfile, src, kept)
            manifest.record("omni", item, None, path=kept, reused=True, kept=True, settles=settles)
            manifest.record("guards", item, None, path=kept, kept=True)
            if icons is not None:
                # No icon in this part (it is the version's clean part): how
                # it is made again, handed on for the next pass — its files
                # in the bucket too.
                rp = replays[j]
                rec = rp.record()
                rec["files"] = {k: dict(v) for k, v in rec["files"].items()}
                await _keep_files(ctx, name, j, rec["files"])
                manifest.record("icons", item, None, path=rp.edited, span=list(icons.spans[j]), stamped=[],
                                kept_from=True, **rec)
            await _step(exp_id, arm, f"{label}: part {j + 1} kept as it was", status="done",
                        detail="nothing new shows in it")
            fitted.append(kept)
            continue
        person = await hbase.run_mask(frame)
        if person is None:
            await _step(exp_id, arm, f"{label}: no speaker mask", status="failed",
                        detail="the new layers are shown over the speaker; the prompt still "
                               "puts them behind them")
        tgt = await _thread(target, frame, out_dir / f"{name}_c{j}_target.png", refs, recipe,
                            person=person)
        # `frame`: what this target was drawn on — the ink of this pass alone
        # is where they differ (base.ink, for a pass added on top of it).
        manifest.record("target", item, st.key(frame=frame, image=tgt), path=tgt, frame=str(frame))
        text = hprompt.scene_prompt(opening, refs, entrance=entering, keep=True, no_icons=icons is not None)
        raw = out_dir / f"{name}_c{j}_raw.mp4"
        k = st.key(chunk=src, prompt=text, images=[tgt], model=omni.OMNI_MODEL,
                   aspect=ctx.aspect, take=take)
        prior = manifest.reusable("omni", item, k)
        t0 = time.monotonic()
        if prior is not None:
            raw.parent.mkdir(parents=True, exist_ok=True)
            if prior.resolve() != raw.resolve():
                await _thread(shutil.copyfile, prior, raw)
            res = {"path": raw}
            await _step(exp_id, arm, f"{label}: chunk {j + 1} reused", status="done",
                        detail="nothing Omni is shown or told changed")
        else:
            res = await omni.edit_video(src, text, aspect=ctx.aspect, images=[tgt], out=raw)
        manifest.record("omni", item, k, path=Path(res["path"]), reused=prior is not None,
                        seconds=time.monotonic() - t0, entering=entering, settles=settles)
        clip = await _thread(
            media.fit_video_to_audio, res["path"], chunk, out_dir / f"{name}_c{j}.mp4",
            width=ctx.width, height=ctx.height,
        )
        g = stack_guards(recipe, refs, entering=entering, veil_keep=keep_ink)
        try:
            extra: dict[str, Any] = {"backdrop": src, "hold": hold}
            if g.veil_keep:
                extra["veil_keep"] = await _thread(veil_keep, frame, refs, recipe)
            if icons is not None:
                clip = await _pass_with_icons(ctx, arm, label, j, chunk, clip, g, icons, manifest, out_dir,
                                              strict=True, record={"hold": True}, **extra)
            else:
                clip = await _thread(gd.apply, clip, chunk, out_dir / f"{name}_c{j}_front.mp4", g,
                                     cuts=await _thread(media.cuts_of, chunk), **extra)
        except Exception as e:
            log.exception("[rebrand] keeping the version / the speaker failed on %s chunk %d",
                          label, j)
            await _step(exp_id, arm, f"{label}: version #{bv.version_id} not kept", status="failed",
                        detail=f"part {j + 1}: {str(e)[:160]}")
            raise RuntimeError(f"keeping version #{bv.version_id} as it was failed on part {j + 1}: "
                               f"{str(e)[:160]}") from e
        if icons is None:
            manifest.record("guards", item, None, path=Path(clip), hold=True, **g.public())
        fitted.append(clip)
    out = fitted[0] if len(fitted) == 1 else await _thread(
        media.concat, fitted, out_dir / f"{name}.mp4",
    )
    patch: dict[str, Any] = {"provider": omni._preferred}
    if icons is not None:
        patch["warnings"] = list(icons.warnings)
    await store.patch_arm(exp_id, arm, patch)
    await _step(exp_id, arm, f"{label}: edited", status="done", detail=f"via {omni._preferred}")
    return out
