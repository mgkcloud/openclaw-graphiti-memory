#!/bin/bash
# start-graphiti.sh — Graphiti startup with Gemini Embedding 2
#
# Gemini Embedding 2 provides state-of-the-art multimodal embeddings
# and is significantly cheaper than OpenAI embeddings ($0.0001/1K chars vs $0.00013/1K tokens).
#
# This script patches OpenAIEmbedder to use Gemini Embedding 2 at startup.
#
set -e

GEMINI_API_KEY="${GEMINI_API_KEY:-${GEMINI_FALLBACK_KEY:-}}"
MODEL_NAME="${MODEL_NAME:-gpt-4.1-mini}"

if [ -z "$GEMINI_API_KEY" ]; then
    echo "[start-graphiti.sh] WARNING: GEMINI_API_KEY not set. Embeddings will fail." >&2
fi

python3 - << 'PATCH_EOF'
import sys, os, json, urllib.request, urllib.error

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL_NAME = os.environ.get("MODEL_NAME", "gpt-4.1-mini")

async def _gemini_create(self, input_data):
    """Patch OpenAIEmbedder.create to use Gemini Embedding 2."""
    if isinstance(input_data, str):
        input_data = [input_data]
    results = []
    for text in input_data:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-embedding-2-preview:embedContent?key=" + GEMINI_API_KEY
        )
        payload = {
            "model": "models/gemini-embedding-2-preview",
            "content": {"parts": [{"text": text}]},
            "taskType": "RETRIEVAL_DOCUMENT",
            "output_dimensionality": 1536,  # matches OpenAI ada-002 dimension count
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                if "embedding" in data:
                    results.append(data["embedding"]["values"])
                elif "embeddings" in data:
                    for e in data["embeddings"]:
                        if "values" in e:
                            results.append(e["values"])
                else:
                    raise RuntimeError(f"Gemini Embedding 2 unexpected response: {data}")
        except Exception as ex:
            print(f"[Gemini Embedding 2] Error: {ex}", file=sys.stderr)
            raise
    return results

# Monkey-patch graphiti's OpenAIEmbedder at import time
try:
    from graphiti_core.embedder.openai import OpenAIEmbedder
    OpenAIEmbedder.create = _gemini_create
    print(
        "[start-graphiti.sh] Gemini Embedding 2 patch applied: "
        f"gemini-embedding-2-preview ({MODEL_NAME} for entity extraction)",
        file=sys.stderr,
    )
except ImportError as exc:
    print(f"[start-graphiti.sh] Could not patch OpenAIEmbedder: {exc}", file=sys.stderr)

sys.path.insert(0, "/app")
import uvicorn

uvicorn.run(
    "graph_service.main:app",
    host="0.0.0.0",
    port=8000,
    reload=False,
    log_level="info",
)
PATCH_EOF
