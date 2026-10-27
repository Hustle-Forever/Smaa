// Rebrand studio — the icon plan for the draft, on its own clock.
//
// The plan (which icons, on which words, where, in what colours) comes from
// POST /icons/plan and NEVER waits on the pattern preview: its timing needs
// no pattern, and its place and colour, until that preview is made, are the
// plan's best guess (`provisional`, re-asked every 2 s and whenever a new
// pattern preview lands, until they are final).
//
//   shown     the last READY plan, with the key of the draft it was made for;
//             "current" only while that is the draft's key. An older one
//             stays on screen while the new one is on its way (it is a plan
//             of the same scene, a click behind), so nothing jumps.
//   status    off       the draft has none of our icons (or none at all)
//             waiting   an edit is waiting out the short debounce
//             loading   the request is under way
//             warming   the scene's first plan: the words, the spots and the
//                       AI are being worked out — asked again every second,
//                       the steps shown as they finish
//             error     422: the server refuses it (its words); else Try again
//             idle      shown is the draft's
//   updating  "Updating icons…" — only once a change has taken over 300 ms
//
// Later plans take milliseconds; going back to one seen this session (an
// undo) is instant (an LRU of final plans). A plan Generate got back with a
// 409 ("the icons changed since you looked") is adopted as the draft's —
// when the draft is still the one that was sent.
//
// Generate needs the FINAL plan: a provisional one is re-asked at once when
// the draft's pattern preview is in (its place and colour are final then),
// unless that preview wasn't kept — then the plan Generate makes is that
// same guess, and the provisional one is the one to send.
//
// "Ask the AI again" shows its own progress (`asking`, with the steps) until
// the new take answers; the server starts no second take while one asks.

import { useCallback, useEffect, useRef, useState } from "react";

import { rebrandErrorDetail } from "../../../lib/rebrandApi";
import {
  harnessApi,
  HarnessHttpError,
  recipeKey,
  viewKey,
  type HarnessBaseRef,
  type HarnessIconPlan,
  type HarnessRecipe,
  type SceneView,
} from "../../../lib/rebrandHarnessApi";
import { iconsOn } from "../../../lib/rebrandIcons";
import { COPY } from "./studioCopy";

const DEBOUNCE_MS = 150;
const WARM_EVERY_MS = 1000;
const PROVISIONAL_EVERY_MS = 2000;
const UPDATING_AFTER_MS = 300;
const TIMEOUT_MS = 45_000;
const CACHE_SIZE = 40;

export type IconPlanStatus = "off" | "idle" | "waiting" | "loading" | "warming" | "error";

export interface ShownIconPlan {
  key: string;
  recipe: HarnessRecipe;
  plan: HarnessIconPlan;
}

export interface IconPlanState {
  /** The draft has our icons on (so a plan is wanted). */
  on: boolean;
  shown: ShownIconPlan | null;
  plan: HarnessIconPlan | null;
  current: boolean;
  status: IconPlanStatus;
  /** The scene's first plan, still warming: its steps. */
  warming: HarnessIconPlan | null;
  /** When the warming started (for m:ss). */
  warmingSince: number | null;
  updating: boolean;
  error: { message: string; retryable: boolean } | null;
  /** Ready for Generate: the draft's own plan, with final place and colour
   *  (a provisional one only when the draft's pattern preview wasn't kept:
   *  Generate's own plan is then the same guess). */
  ready: boolean;
  /** "The icons changed since you looked — check them." */
  notice: string | null;
  dismissNotice: () => void;
  retry: () => void;
  /** A new take of the AI's choices (after it was unavailable). */
  askAgain: () => void;
  /** "Ask the AI again" is under way for this draft: its steps meanwhile. */
  asking: boolean;
  askingSteps: HarnessIconPlan | null;
  askingSince: number | null;
  /** A plan from Generate's 409: shown as the draft's when `sent` (what was
   *  posted) is still the draft; else only the notice. */
  adopt: (plan: HarnessIconPlan, notice: string, sent: { recipe: HarnessRecipe; base: HarnessBaseRef | null }) => void;
}

/** The draft as the plan depends on it (the key of the request): the
 *  scene's end set by hand joins it only when there is one. */
function planKeyOf(draft: HarnessRecipe | null, base: HarnessBaseRef | null, view: SceneView | null = null): string {
  return draft ? recipeKey(draft) + (base ? `@${base.mode}:${base.version_id}` : "") + viewKey(view) : "";
}

export function useIconPlan({
  runId,
  target,
  draft,
  base = null,
  enabled,
  patternCurrent,
  patternUnkept = false,
  patternKey,
  view = null,
}: {
  runId: number;
  target: string;
  draft: HarnessRecipe | null;
  base?: HarnessBaseRef | null;
  /** The scene's end set by hand (sent, and keyed, only when set). */
  view?: SceneView | null;
  /** The scene is prepared and the server draws icons. */
  enabled: boolean;
  /** The pattern preview on screen is the draft's. */
  patternCurrent: boolean;
  /** ... and it wasn't kept (drawn with a problem): the icons' place and
   *  colour stay a guess, at Generate too. */
  patternUnkept?: boolean;
  /** Changes whenever a new pattern preview lands (re-asks a provisional
   *  plan at once). */
  patternKey: string | null;
}): IconPlanState {
  const on = enabled && !!draft && iconsOn(draft);
  const key = on ? planKeyOf(draft, base, view) : "";
  const viewRef = useRef(view);
  viewRef.current = view;
  const [shown, setShown] = useState<ShownIconPlan | null>(null);
  const shownRef = useRef(shown);
  shownRef.current = shown;
  const [warming, setWarming] = useState<{ key: string; plan: HarnessIconPlan; since: number } | null>(null);
  const [error, setError] = useState<{ key: string; message: string; retryable: boolean } | null>(null);
  const [inflight, setInflight] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const [slow, setSlow] = useState(false);
  const [asking, setAsking] = useState<{ key: string; since: number } | null>(null);
  const cache = useRef(new Map<string, HarnessIconPlan>());
  // Read inside a request's own clock (a provisional answer re-asks at once
  // when the draft's pattern preview is in and kept).
  const patternRef = useRef({ current: patternCurrent, unkept: patternUnkept });
  patternRef.current = { current: patternCurrent, unkept: patternUnkept };

  const remember = (k: string, p: HarnessIconPlan) => {
    const c = cache.current;
    c.delete(k);
    c.set(k, p);
    while (c.size > CACHE_SIZE) {
      const oldest = c.keys().next().value;
      if (oldest === undefined) break;
      c.delete(oldest);
    }
  };

  const keyRef = useRef(key);
  keyRef.current = key;

  // A new pattern preview re-asks a provisional plan at once (its place and
  // colour can be final now).
  const [refresh, setRefresh] = useState(0);
  const lastPattern = useRef(patternKey);
  useEffect(() => {
    if (patternKey === lastPattern.current) return;
    lastPattern.current = patternKey;
    const cur = shownRef.current;
    if (cur && cur.key === keyRef.current && cur.plan.provisional) setRefresh((n) => n + 1);
  }, [patternKey]);

  // "Ask the AI again": the next request carries ask_again, once.
  const again = useRef(false);

  useEffect(() => {
    if (!on || !draft) return;
    const k = key;
    const cur = shownRef.current;
    if (cur?.key === k && !cur.plan.provisional && !again.current) return;
    const hit = again.current ? undefined : cache.current.get(k);
    if (hit) {
      const next = { key: k, recipe: draft, plan: hit };
      shownRef.current = next;
      setShown(next);
      setError(null);
      return;
    }
    const recipe = draft;
    const on_ = base;
    const seen = view;
    const ctl = new AbortController();
    let cancelled = false;
    let timer: number | undefined;
    let limit: number | undefined;
    // A provisional answer with the draft's pattern preview already in is
    // asked again at once — once; after that on the usual clock.
    let quick = true;

    const ask = async () => {
      window.clearTimeout(limit);
      limit = window.setTimeout(() => ctl.abort(), TIMEOUT_MS);
      setInflight(k);
      const askAgain = again.current;
      again.current = false;
      try {
        const p = await harnessApi.iconPlan(runId, target, recipe, { base: on_, signal: ctl.signal, askAgain, view: seen });
        if (cancelled) return;
        if (p.state === "warming") {
          setWarming((w) => (w && w.key === k ? { ...w, plan: p } : { key: k, plan: p, since: Date.now() }));
          timer = window.setTimeout(() => void ask(), WARM_EVERY_MS);
          return;
        }
        setWarming((w) => (w && w.key === k ? null : w));
        setAsking((a) => (a && a.key === k ? null : a));
        if (!p.provisional) remember(k, p);
        const next = { key: k, recipe, plan: p };
        shownRef.current = next;
        setShown(next);
        setError(null);
        if (p.provisional) {
          const pat = patternRef.current;
          const now = quick && pat.current && !pat.unkept;
          if (now) quick = false;
          timer = window.setTimeout(() => void ask(), now ? 0 : PROVISIONAL_EVERY_MS);
        }
      } catch (e) {
        if (cancelled) return;
        setAsking((a) => (a && a.key === k ? null : a));
        const refused = e instanceof HarnessHttpError && (e.status === 422 || e.status === 400);
        const message = ctl.signal.aborted
          ? COPY.icons.iconsTooLong
          : refused
            ? (e as HarnessHttpError).errors.map((x) => x.message).join("; ") || (e as HarnessHttpError).detail
            : e instanceof TypeError
              ? COPY.offline
              : e instanceof HarnessHttpError && e.status >= 500
                ? COPY.serverError
                : rebrandErrorDetail(e);
        setError({ key: k, message, retryable: !refused });
      } finally {
        window.clearTimeout(limit);
        if (!cancelled) setInflight((x) => (x === k ? null : x));
      }
    };

    const first = !cur;
    timer = window.setTimeout(() => void ask(), first || cur?.key === k ? 0 : DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      window.clearTimeout(limit);
      ctl.abort();
    };
    // draft and base are read through key: the same key is the same recipe
    // on the same version.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [on, key, nonce, refresh, runId, target]);

  const current = !!shown && shown.key === key;
  const err = error && error.key === key ? error : null;
  const warm = warming && warming.key === key && !current ? warming : null;
  const status: IconPlanStatus = !on
    ? "off"
    : current
      ? "idle"
      : err
        ? "error"
        : warm
          ? "warming"
          : inflight === key
            ? "loading"
            : "waiting";

  // "Updating icons…" only once a change has taken a moment.
  const pending = on && !!shown && (status === "waiting" || status === "loading");
  useEffect(() => {
    setSlow(false);
    if (!pending) return;
    const t = window.setTimeout(() => setSlow(true), UPDATING_AFTER_MS);
    return () => window.clearTimeout(t);
  }, [pending, key]);

  // An "Ask the AI again" is for the draft it was asked on.
  useEffect(() => {
    setAsking((a) => (a && a.key !== key ? null : a));
  }, [key]);

  // A notice about an older plan goes once the draft moves on.
  const noticeKey = useRef<string | null>(null);
  useEffect(() => {
    if (notice && noticeKey.current !== key) setNotice(null);
  }, [key, notice]);

  const retry = useCallback(() => {
    setError(null);
    setNonce((n) => n + 1);
  }, []);

  const askingRef = useRef(asking);
  askingRef.current = asking;
  const askAgain = useCallback(() => {
    const k = keyRef.current;
    // Once per take: a click while it asks does nothing (nor does the server).
    if (askingRef.current?.key === k) return;
    again.current = true;
    cache.current.delete(k);
    setAsking({ key: k, since: Date.now() });
    setError(null);
    setNonce((n) => n + 1);
  }, []);

  const draftRef = useRef(draft);
  draftRef.current = draft;
  const adopt = useCallback(
    (plan: HarnessIconPlan, text: string, sent: { recipe: HarnessRecipe; base: HarnessBaseRef | null }) => {
      const d = draftRef.current;
      if (!d) return;
      // A plan for the recipe that was sent: the draft's only if it is still
      // that one (an edit made meanwhile gets its own plan, as usual).
      if (planKeyOf(sent.recipe, sent.base, viewRef.current) === keyRef.current) {
        const next = { key: keyRef.current, recipe: d, plan };
        if (!plan.provisional) remember(keyRef.current, plan);
        shownRef.current = next;
        setShown(next);
      }
      noticeKey.current = keyRef.current;
      setNotice(text);
    },
    [],
  );

  const plan = shown?.plan ?? null;
  const ready =
    !on ||
    (current &&
      !!plan &&
      plan.state === "ready" &&
      !!plan.plan_key &&
      (!plan.provisional || (patternCurrent && patternUnkept)));
  const askingNow = on && !!asking && asking.key === key;

  return {
    on,
    shown,
    plan,
    current,
    status,
    warming: warm?.plan ?? null,
    warmingSince: warm?.since ?? null,
    updating: pending && slow,
    error: err ? { message: err.message, retryable: err.retryable } : null,
    ready,
    notice,
    dismissNotice: () => setNotice(null),
    retry,
    askAgain,
    asking: askingNow,
    askingSteps: askingNow && warming?.key === key ? warming.plan : null,
    askingSince: askingNow ? asking!.since : null,
    adopt,
  };
}
