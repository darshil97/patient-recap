"""v1 router assembly."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import audio_uploads, follow_ups, health, jobs, patients, stats, visits

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(stats.router)
api_router.include_router(patients.router)
api_router.include_router(visits.router)
api_router.include_router(follow_ups.router)
api_router.include_router(jobs.router)
api_router.include_router(audio_uploads.router)
