"""Minimal hello-world FastAPI app for verifying deploy + CORS plumbing.

Deploy this FIRST. Once you can fetch / from your friend's Lovable frontend
without CORS errors, you know the URL and CORS are right. Then switch your
deploy's start command to `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
(after configuring all the real env vars).

Run locally:
    pip install fastapi uvicorn
    uvicorn hello:app --reload

Run on Railway/Render:
    Start command: uvicorn hello:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Syllabus-to-Calendar API (hello)")

# Permissive CORS for the hello-world phase. When you flip to the real app,
# the main app reads a stricter ALLOWED_ORIGINS list from env.
allowed = os.getenv("ALLOWED_ORIGINS", "*")
if allowed == "*":
    origins = ["*"]
else:
    origins = [o.strip() for o in allowed.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,  # must be False when allow_origins=["*"]
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root() -> dict:
    """Liveness probe. Returns {"status": "ok"}."""
    return {"status": "ok"}


@app.get("/health")
async def health() -> dict:
    """Same thing, different path — some platforms probe /health."""
    return {"status": "ok"}
