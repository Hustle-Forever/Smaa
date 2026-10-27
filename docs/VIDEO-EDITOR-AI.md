# Video Editor AI — the agentic editor

## What changed, and why

The old engine was a **blind one-shot planner**. A `gemini-2.5-flash` call with
`thinking_budget=0` was handed a JSON summary of the timeline and asked to emit a
whole edit in one shot — having never seen a single frame of the footage.
Everything it got wrong was then patched up by a large deterministic repair layer
(`_backfill_framed_popup_defaults`, `_force_attached_font`, `_mark_intro_title_card`,
`_enforce_bookend_order`, …). The result was effectively template-filling with an
LLM choosing the words, which is why every video came out the same shape.

Worse: a proper agent (`yas_video_edit_agent.py`) had already been written and
committed, and **nothing could reach it** — no route, not in `_CHAT_FEATURES`, no
frontend call. 100% of user traffic went to the blind planner.

The editor is now an actual agent:

| | Before | Now |
|---|---|---|
| Can it see the video? | No — text digest only | Yes — real frames on demand (`look`) |
| Can it see its own work? | No | Yes — Remotion still render (`preview`) |
| Reasoning tokens | Zero everywhere | Thinking on, effort `high` |
| Loop | Single shot | Bounded look → think → act → check |
| Output shape | Fixed template | Decided by the content |
| Reachable from the UI | No | `/ai/agent`, chat routing, one-click edit |

---

## The reasoning brain (`services/model_gateway.py`)

Provider availability differs per deployment, so the gateway **probes and caches**:

```
1. vertex-claude     Claude on Vertex AI via the project's ADC / service account.
                     Preferred: no extra key, separate quota pool from Gemini.
2. anthropic-claude  The direct Anthropic API (needs ANTHROPIC_API_KEY).
3. gemini            Gemini with native function calling + thinking ON, running
                     the SAME tool loop. The floor — the editor is never dumber
                     than an agent, even with no Claude at all.
```

Check which one is live at any moment:

```
GET /api/yas-producer/video-editor/ai/providers          # cached verdict
GET /api/yas-producer/video-editor/ai/providers?refresh=true   # re-probe
```

The UI shows this as a small badge in the AI panel.

### Enabling Claude on Vertex (the preferred path)

Claude-on-Vertex needs **two** things on the GCP project, and 403s without both:

1. **Billing enabled** on the project.
2. **Claude enabled once** in Vertex AI Model Garden, in the region you point at
   (`CLAUDE_VERTEX_REGION`, default `us-east5`).

Verify from the backend venv:

```bash
cd backend
.venv/Scripts/python.exe -c "import asyncio,sys; sys.path.insert(0,'.'); \
from app.services.model_gateway import reasoner_status; \
print(asyncio.run(reasoner_status()))"
```

### Falling back to the Anthropic API

If Claude-on-Vertex can't be enabled, put a key in `backend/.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-opus-5
```

The gateway picks it up on the next probe (or immediately with `?refresh=true`).

### Pinning a provider

```
VE_AGENT_PROVIDER=auto | vertex-claude | anthropic-claude | gemini
VE_AGENT_GEMINI_MODEL=gemini-2.5-pro
VE_AGENT_MAX_STEPS=24
```

---

## The agent (`services/yas_video_edit_agent.py`)

Twelve tools, in the order it typically uses them:

| Tool | What it does |
|---|---|
| `inspect_timeline` | Every clip with real ids, times in **seconds**, media bin, popups on screen |
| `read_transcript` | What is said, with timings |
| `look(at_s)` | **The actual frame** at a timeline moment + scene/mood/face info |
| `suggest_overlay_slots` | Face-avoiding placements computed from the footage |
| `search_assets` | Semantic search over the brand library |
| `get_popup_examples` | How the brand's best Instagram posts use popups |
| `read_edit_log` | Edits already made, so it doesn't redo work |
| `generate_image` | Creates a new sticker (Nano Banana) → a real library asset |
| `run_pass` | The deterministic passes: `captions`, `duck`, `silence`, `music` |
| `edit(ops)` | Applies validated ops |
| `preview(at_s)` | **Renders the current edit** and looks at it, to fix its own work |
| `finish(summary)` | Ends the run with a user-facing summary |

`preview` needs the Remotion `/still` endpoint added in this change. If the
Remotion service hasn't been rebuilt, `preview` reports itself unavailable and
the agent falls back to `look` — it degrades, it does not break.

### Invariants this module must keep

These were derived from a deep read of the orchestrator; breaking any one
corrupts projects:

1. **Never call `start_job` from inside a job.** One job slot per project — the
   guard would 409 against our own running job and kill it.
2. **Never call the `_run_*` feature helpers.** Each writes a terminal
   `status: done` into the shared job blob, which ends the parent job in the UI
   mid-flight and un-pauses the browser's autosave, which then clobbers whatever
   the agent does next. Call their *pure cores* instead (`run_pass` does).
3. **Serialise applies.** `apply_plan → save_timeline` is last-write-wins with no
   `base_rev` check; two concurrent applies silently lose one.
4. **Random plan ids for additive work.** Stable ids (`captions{pid}`) make
   `apply_plan` sweep that plan's previous clips first.
5. **Re-ground after every apply.** Stale clip ids are the #1 rejection cause.

---

## How work reaches the agent

| Entry point | Behaviour |
|---|---|
| `POST /ai/agent` | Direct: `{instruction, clip_id?}` |
| Chat | The router sends judgement/on-screen/multi-step asks to `agent`; trivial exact edits still take the fast op path |
| One-click **Edit my video** | Uses the agent when a brain is reachable; falls back to the classic two-phase template otherwise |

Progress streams into the job blob as `agent_steps` (tool + one-line result), so
the UI shows what the AI is actually doing rather than a spinner.

---

## Safety rails

The agent holds the project's single job slot while it runs, so it is bounded on
three axes: `VE_AGENT_MAX_STEPS` tool round-trips (default 24), a 12-minute
wall-clock deadline, and per-run caps of 12 `look`s and 6 `preview`s. Renders are
serialised globally in the Remotion service, so a preview can never start Chrome
alongside an in-flight export.

A provider that fails a real run non-retryably is demoted for 30 minutes, so a
capability mismatch falls through to the next brain instead of failing every
request. `/ai/status` reports the server's authoritative `job_active`, so a job
orphaned by a backend restart can't leave the editor's autosave paused.

## Verification status

Verified on this machine:

- **271 backend tests pass**; `npx tsc -b --force --noEmit` clean for the
  frontend; `tsc --noEmit` clean for the Remotion service.
- Routes registered; every module imports; the frame maths, clip-layer
  selection, schema translation and the op contract are exercised directly —
  including a test asserting that **every op the agent advertises actually
  passes the validator** (the check that caught `mute_clip`).

**Not** verified live: this dev machine has no working credentials — the ADC
project has billing disabled (every Vertex model, Claude *and* Gemini, returns
403) and the Supabase pool is unreachable. So no real model call and no real
project edit was exercised here.

First run on a working environment:

1. `GET /api/yas-producer/video-editor/ai/providers?refresh=true` — confirm which
   brain answered, and read `reason` if `ok` is false.
2. Give the agent one small, checkable instruction on a real project (e.g.
   "trim the dead air at the start") and watch the step list.
3. Then try the one-click **Edit my video**.
