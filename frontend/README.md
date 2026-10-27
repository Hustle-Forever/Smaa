# Frontend (placeholder)

The Next.js UI is out of scope for Agent 1 v1. Once the backend pipeline is
running end-to-end, this folder will house the Next.js 14 app that calls the
FastAPI backend at `http://127.0.0.1:8000`.

The plan is to port the existing 5-route layout (`/`, `/website`, `/submissions`,
`/brand`, `/settings`) from `../../Archivist/frontend/` and add:

- **Live progress** on the `/website` page via SSE
  (`GET /api/agents/website-ingest/runs/{id}/events`)
- A **Run** button on `/` and `/website` that calls
  `POST /api/agents/website-ingest/run`
- Replace the "Open in n8n ↗" external links with native run buttons
