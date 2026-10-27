"""Producer Agent — brand-aware AI video generation via Vertex AI + Veo 3.1.

Unlike the ingest agents (long-running pipelines), the Producer is a state
machine. Each REST endpoint advances a producer_runs row through:
    started → ideas_pending → idea_approved
            → script_pending → script_approved
            → video_pending → video_ready
            → completed

Each step pauses for user approval. Declines are vectorised so the next
generation can learn what the user dislikes.
"""
