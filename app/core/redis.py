"""
core/redis.py
─────────────
Async Redis client singleton using redis.asyncio.
Handles connection pooling and graceful reconnect.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator
from contextlib import asynccontextmanager
from fastapi import FastAPI

import redis.asyncio as aioredis
from redis.asyncio import Redis

from app.core.config import get_settings

logger   = logging.getLogger(__name__)
settings = get_settings()

_redis_client: Redis | None = None


async def get_redis() -> Redis:
    """
    Returns a connected async Redis client.
    Creates the pool on first call (lazy init).
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_keepalive=True,
            health_check_interval=30,
        )
        logger.info(f"Redis pool created → {settings.redis_url}")
    return _redis_client


async def close_redis() -> None:
    global _redis_client
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None
        logger.info("Redis pool closed")


@asynccontextmanager
async def redis_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan helper — use in app startup/shutdown."""
    yield
    await close_redis()