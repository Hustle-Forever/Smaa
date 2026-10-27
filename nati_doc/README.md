# nati_doc

Day-to-day progress on the 42 Studio frontend (UI/UX).

## Working rules

1. **Frontend only.** Nothing under `backend/` is changed. Every screen must keep working with the API exactly as it is.
2. **Every working day gets a log here**, named `YYYY-MM-DD.md`, with before/after screenshots in `assets/YYYY-MM-DD/`.
3. **Verified before it is called done.** Each change is type-checked, built, and screenshotted with real-shaped data and with empty data.

## Design report for the team

[Design_by_nhatty.md](Design_by_nhatty.md) has every change, the words we changed, open questions, and a table for team comments.

## Team updates

- [28 Sep 2026: what was done and the plan for the next 15 hours](TEAM_UPDATE_2026-09-28.md)

## Log

| Day | What changed |
|---|---|
| [2026-09-28](2026-09-28.md) | Dashboard redesign and sidebar polish. Round 2: Apple-style glass instead of black, a new analysis chart, a "Next step" card, and plain-language labels. Round 3: real glass on a square-grid colour canvas, ink icons in glass wells, a chart with Per day / Running total, and the `?demo` sample preview. Round 4: professional government-office look: flat grid background, ink and white, gold as the only accent. Round 5: round share gauges on the three pipeline steps. Round 6: quiet motion (count-up numbers, chart draw-in, staggered reveal). Round 7: very slight blur on the background grid. **Assets page redesign** and **Brand profile redesign** (both ready for team review). **Ideation redesign** (waiting for Nati's review) |
| [2026-10-07](2026-10-07.md) | Studied 42abudhabi.ae. Built the Dashboard and sidebar in **42 Dark** and **42 Light**. Round 2: real 42 look (square, outlined cyan buttons, purple blocks, Futura caps) on **every page**, both themes, a Dark/Light switch. Meeting brief: [MEETING_2026-10-08_42_BRANDING.md](MEETING_2026-10-08_42_BRANDING.md). Round 3: **Library page** fully redesigned (summary tiles, search, square tiles, Scheduled tab). Round 4: **42 website components** (stacked cards, pixel icons, pixel headline, line-art map, purple stats block, STEP cards) on Dashboard, Ideation, Library, Assets and Brand, plus a strong test (contrast, keyboard, 3 sizes, reduced motion). Round 5: Nati didn't like the Round 4 cards, so they are **back to before**; only the **map line background** stays, now soft and behind **every page**. Round 6: header buttons fixed (solid, no map lines through them), the running light on the map is back, charts draw in when you scroll to them, and the UI can be reviewed with the **real studio data** |
| [2026-10-11](2026-10-11.md) | Dashboard totals are clickable: each opens a detail window that grows out of its tile, with a 30-day chart, a status ring, a breakdown and the latest pieces |
| [2026-10-14](2026-10-14.md) | Sign-in page redesigned in 42 style: charcoal brand panel with the animated city map, a new form with show password, Caps Lock warning and clear errors, and a Dark/Light switch. Round 2: the 4 detail windows match the Assets style, SMAA story detail rebuilt, and the leadership meeting doc. Round 3: the "This week" planner was removed at Nati's request. Round 4: the Brand profile is now a visual brand book (colours always painted, sample post, mood board with real pictures, font samples, phrase posters, rules checklist). Round 5: "Human Character" button is now "AI-generated character" with a person icon |

## How to run it locally

```
# backend (port 8000 is taken on this machine, so 8010)
cd backend
.venv\Scripts\activate
set API_PORT=8010
python run.py

# frontend (the dev server proxies /api to API_PORT)
cd frontend
set API_PORT=8010
npm run dev          # http://127.0.0.1:4242
```

Before merging any UI change:

```
cd frontend
npm run typecheck
npm run build
```
