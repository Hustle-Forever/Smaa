# UI/UX update · Monday 28 September 2026

**From:** Nati (UI/UX)
**Branch:** `nati/ui-redesign-2026-09-28` (not merged into `main`)
**Scope:** frontend only. **No backend file was changed.** Every API call is the same as before.

---

## What I did today

The goal was a professional, government-office look that anyone can use without training: ink black, white, and one gold accent, on a soft square-grid background.

| Page | What changed | Status |
|---|---|---|
| **Dashboard** | Fully redesigned over 7 rounds of feedback:<br>- A "Next step" card and the "Where your posts are" pipeline with round progress gauges<br>- Clear totals and a real analysis chart with Per day / Running total<br>- Plain-word labels and quiet motion: numbers count up and the chart draws in | Done |
| **Sidebar** | White active pill, bigger logo, a "Beta" tag that no longer breaks labels, and a cleaner account card | Done |
| **Assets** | Groups show a peek of their real pictures<br>- Search with a clear button<br>- A 3-step upload window<br>- Plain words in the asset window ("Active / Paused")<br>- Friendly empty and error states | Ready for review |
| **Brand · Profile** | The messy text is now a clean brand book:<br>- Summary card and "Jump to" links<br>- Colours you can click to copy<br>- Font samples, audience and theme cards, quote cards<br>- Always / Never rules | Ready for review |
| **Ideation** | New hub with a drawing for each tool and a "what you get" tag<br>- A new roadmap bar with plain step hints<br>- All 7 tools re-skinned with readable labels, the ink accent and quiet cards, **without changing any tool code** | Waiting for feedback |

**How it was checked, every time:**
- Type check and production build pass.
- Real-browser screenshots on desktop and phone, using realistic test data and the real (empty) local server.
- No page errors and no sideways scrolling.

Full details, before/after screenshots and the list of renamed words are in [`Design_by_nhatty.md`](Design_by_nhatty.md).

---

## For developers: good to know before you pull

- **Backend untouched:** nothing under `backend/` changed. No new endpoints, no new request fields.
- **Accent colour is now a CSS variable:** `--accent-rgb` in `styles.css`.
  - The default is still teal `6 182 162`, so pages that were not redesigned look exactly the same. This was measured: `rgb(6, 182, 162)` on Settings and Library.
  - Inside `.studio-ui` (Ideation) it is ink.
- **New shared pieces:**
  - Styles: `.glass`, `.glass-inset`, `.icon-well`, `.segmented`, `.illo-panel`, `.studio-ui`
  - Colour tokens: `gold` / `gold-ink` / `gold-soft`
  - Hook: `useStudioCanvas()`
  - Animations: `animate-reveal`, `animate-drawLine`
- **Local sample data:** `http://127.0.0.1:4242/?demo#dashboard` shows the Dashboard with sample numbers. It only works on the dev server; production builds never show it.
- **Restart `npm run dev` after changing `tailwind.config.ts`.** Otherwise new colours show up as default blue.
- **Label changes need the team's OK:** please check the "Words we changed" table in `Design_by_nhatty.md`. The data keys are unchanged; only the words people read changed.

---

## Plan for the next 15 hours of work

In order of priority:

| # | Task | Why | Est. |
|---|---|---|---|
| 1 | **Apply Nati's and the team's feedback on Ideation, Assets and Brand** | Feedback comes first. Anything the team dislikes in `Design_by_nhatty.md` gets fixed before new work starts | 3 h |
| 2 | **Human Element Video Studio, inside:** fit its panel to the page grid and restyle its inner steps (Topic → Look → Stills → Videos → Final) | It is the most used video tool and still has the old step chips | 3 h |
| 3 | **Plain words for the technical lines inside tools**, for example "gpt-4o + Nano Banana Pro · ~$0.55" → "Makes 4 versions · about 1 minute · about $0.55" | Non-technical staff shouldn't have to read model names | 1.5 h |
| 4 | **Library page** redesign in the same style (history, the Scheduled queue, "Continue editing") | It is the last big page people use every day | 4 h |
| 5 | **Brand: Upload, Synthesizer and Instagram tabs** get the same look as the Profile tab | Right now only their header and tab switch are new | 2 h |
| 6 | **Small fixes:**<br>- "Add asset" inside a group pre-selects that group<br>- The phone top bar shows the real 42 logo<br>- Review the Video Editor's look (it has dark screens) | Quick wins | 1.5 h |

After each item: type check, build, desktop and phone screenshots, and an entry in `Design_by_nhatty.md` and the daily log.

---

## What I need from the team

1. Look at the screenshots in `Design_by_nhatty.md` and add comments to the **Team feedback** table at the bottom.
2. Confirm the **"Words we changed"** table, or tell me which words to keep.
3. Answer the **Questions for the team**: gold accent OK? switch the teal to ink on every page now?
4. **Please don't merge this branch into `main` yet.** It gets merged after review.
