# 42 Studio — FastAPI backend
#
# Heavy native deps: opencv (scenedetect) needs libGL + glib; yt-dlp/scenedetect
# use ffmpeg; PyTorch/CLIP (sentence-transformers) is CPU-only here. We install
# CPU torch FIRST from the PyTorch CPU index so the default CUDA wheels (several
# GB of nvidia-* packages) are never pulled.
FROM python:3.12-slim

# --- System libraries -------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        libpq5 \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Production defaults (compose overrides these explicitly too):
    API_HOST=0.0.0.0 \
    AUTO_RELOAD=false

WORKDIR /app/backend

# --- Python deps (cached layer) --------------------------------------------
# CPU-only torch first so sentence-transformers reuses it instead of the CUDA build.
# The >=2.4 floor matches requirements.txt: below it, transformers>=5 disables
# torch and `import sentence_transformers` raises "name 'nn' is not defined" (#32).
# Keeping the constraint in the command text is also what invalidates a stale
# cached layer that already holds an older torch.
RUN pip install --upgrade pip \
    && pip install "torch>=2.4" --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Pre-warm the CLIP model (~340 MB) into the image so the first request that
# needs visual search isn't blocked on a download. Matches CLIP_MODEL default.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('clip-ViT-B-32')"

# Pre-fetch the Rebrand icons' word-timing model (openai/whisper-base.en,
# ~290 MB) at the revision rebrand_harness/words.py pins (WHISPER_REVISION —
# change both together), so the first icon plan after a deploy is not blocked
# on the download (the HF cache lives in the container, not the data volume).
# Only the safetensors weights and the config/tokenizer files: the repo also
# holds PyTorch, TF and Flax copies of the same weights.
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('openai/whisper-base.en', revision='911407f4214e0e1d82085af863093ec0b66f9cd6', allow_patterns=['*.json', '*.txt', 'model.safetensors'])"

# --- App code ---------------------------------------------------------------
COPY . .

EXPOSE 8000

# /app/backend/data is provided by a shared volume at runtime (see compose).
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["python", "run.py"]
