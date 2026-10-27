"""Agent 5 — Instagram ingest.

Pulls public media from another Business/Creator IG account via Meta's
Business Discovery API, downloads the media files, runs the existing
image/video understanding pipeline (CLIP + Whisper + GPT-4o vision), and
embeds captions into the 768d Vertex space so the Producer agent can
retrieve high-engagement posts as taste evidence.

See `agent.py` for the orchestrator and `phases/` for the individual steps.
"""
