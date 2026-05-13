"""
tests/test_liveness_challenge.py
─────────────────────────────────
Unit tests for the liveness challenge service.
Uses fakeredis for in-process Redis simulation — no external dependencies.

Run:
    pytest tests/test_liveness_challenge.py -v
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

# ── Fake Redis setup ───────────────────────────────────────────────────────────
# fakeredis provides a full async Redis compatible interface in-process.
try:
    import fakeredis.aioredis as fakeredis
except ImportError:
    pytest.skip("fakeredis not installed — pip install fakeredis", allow_module_level=True)

from app.services.liveness_challenge import (
    REDIS_KEY_PREFIX,
    SESSION_TTL,
    ChallengeVerifyResult,
    _evaluate_entropy,
    _validate_frame_timing,
    generate_challenge,
    verify_challenge,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis()


@pytest.fixture(autouse=True)
def patch_redis(fake_redis):
    """Patch get_redis() globally for all tests in this module."""
    with patch("app.services.liveness_challenge.get_redis", return_value=fake_redis):
        # get_redis is called with `await` in the service — wrap it
        async def _async_fake():
            return fake_redis
        with patch("app.services.liveness_challenge.get_redis", new=_async_fake):
            yield fake_redis


def make_timestamps(n: int = 30, delta_ms: int = 33, jitter: int = 5) -> list[int]:
    """Generate realistic frame timestamps with natural jitter."""
    import random
    base = int(time.time() * 1000)
    ts = [base]
    for _ in range(n - 1):
        ts.append(ts[-1] + delta_ms + random.randint(-jitter, jitter))
    return ts


def make_entropy(good: bool = True) -> dict:
    if good:
        return {
            "brightnessVariance":    2.5,
            "noiseVariance":         1.8,
            "headStabilityVariance": 0.0003,
            "earMicroVariance":      0.0008,
            "blinkLatencyMs":        450.0,
            "turnLatencyMs":         820.0,
        }
    return {
        "brightnessVariance":    0.1,   # suspiciously low
        "noiseVariance":         0.05,  # suspiciously low
        "headStabilityVariance": 0.000001,
        "earMicroVariance":      0.00001,
        "blinkLatencyMs":        100.0,
        "turnLatencyMs":         200.0,
    }


# ── generate_challenge ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_challenge_returns_sequence_and_nonce():
    result = await generate_challenge("sess-001")
    assert "sequence" in result
    assert "nonce" in result
    assert len(result["sequence"]) == 3
    assert len(result["nonce"]) == 48   # 24 bytes hex = 48 chars


@pytest.mark.asyncio
async def test_generate_challenge_stores_in_redis(patch_redis):
    redis = patch_redis
    await generate_challenge("sess-002")
    raw = await redis.get(f"{REDIS_KEY_PREFIX}sess-002")
    assert raw is not None
    data = json.loads(raw)
    assert "nonce" in data
    assert "sequence" in data
    assert "created_at" in data


@pytest.mark.asyncio
async def test_generate_challenge_idempotent_within_ttl():
    """Re-fetching within half the TTL returns the same nonce."""
    r1 = await generate_challenge("sess-003")
    r2 = await generate_challenge("sess-003")
    assert r1["nonce"] == r2["nonce"]
    assert r1["sequence"] == r2["sequence"]


@pytest.mark.asyncio
async def test_generate_challenge_different_sequences():
    """Two different sessions should (almost certainly) get different sequences."""
    results = {
        frozenset((await generate_challenge(f"sess-{i}"))["sequence"])
        for i in range(10)
    }
    # With 5 challenges choose 3 = 60 permutations; 10 sessions should vary
    assert len(results) > 1


# ── verify_challenge ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_challenge_happy_path():
    challenge = await generate_challenge("sess-ok")
    nonce    = challenge["nonce"]
    sequence = challenge["sequence"]

    # Simulate elapsed time > 3s
    with patch("app.services.liveness_challenge.time") as mock_time:
        mock_time.time.return_value = time.time() + 5.0

        result = await verify_challenge(
            session_id=          "sess-ok",
            nonce=               nonce,
            completed_sequence=  sequence,
            frame_timestamps_ms= make_timestamps(),
            entropy=             make_entropy(good=True),
        )

    assert result.ok is True
    assert result.flagged is False
    assert result.error is None


@pytest.mark.asyncio
async def test_verify_challenge_wrong_nonce():
    await generate_challenge("sess-bad-nonce")

    result = await verify_challenge(
        session_id=          "sess-bad-nonce",
        nonce=               "aaaa" * 12,  # wrong nonce
        completed_sequence=  ["blink", "turn_left", "turn_right"],
        frame_timestamps_ms= make_timestamps(),
        entropy=             make_entropy(),
    )

    assert result.ok is False
    assert "nonce" in result.error.lower()


@pytest.mark.asyncio
async def test_verify_challenge_wrong_sequence():
    challenge = await generate_challenge("sess-bad-seq")
    nonce     = challenge["nonce"]
    sequence  = challenge["sequence"]

    # Reverse the sequence — wrong order
    wrong = list(reversed(sequence))

    with patch("app.services.liveness_challenge.time") as mock_time:
        mock_time.time.return_value = time.time() + 5.0
        result = await verify_challenge(
            session_id=          "sess-bad-seq",
            nonce=               nonce,
            completed_sequence=  wrong,
            frame_timestamps_ms= make_timestamps(),
            entropy=             make_entropy(),
        )

    assert result.ok is False
    assert "sequence" in result.error.lower()


@pytest.mark.asyncio
async def test_verify_challenge_too_fast():
    challenge = await generate_challenge("sess-fast")

    with patch("app.services.liveness_challenge.time") as mock_time:
        mock_time.time.return_value = time.time() + 1.0  # only 1 second elapsed

        result = await verify_challenge(
            session_id=          "sess-fast",
            nonce=               challenge["nonce"],
            completed_sequence=  challenge["sequence"],
            frame_timestamps_ms= make_timestamps(),
            entropy=             make_entropy(),
        )

    assert result.ok is False
    assert "quickly" in result.error.lower() or "fast" in result.error.lower()


@pytest.mark.asyncio
async def test_verify_challenge_expired():
    challenge = await generate_challenge("sess-expired")

    with patch("app.services.liveness_challenge.time") as mock_time:
        mock_time.time.return_value = time.time() + SESSION_TTL + 5

        result = await verify_challenge(
            session_id=          "sess-expired",
            nonce=               challenge["nonce"],
            completed_sequence=  challenge["sequence"],
            frame_timestamps_ms= make_timestamps(),
            entropy=             make_entropy(),
        )

    assert result.ok is False
    assert "expired" in result.error.lower()


@pytest.mark.asyncio
async def test_verify_challenge_nonce_burned_after_use():
    """After successful verification, the nonce must not work again."""
    challenge = await generate_challenge("sess-burn")

    with patch("app.services.liveness_challenge.time") as mock_time:
        mock_time.time.return_value = time.time() + 5.0

        r1 = await verify_challenge(
            session_id=          "sess-burn",
            nonce=               challenge["nonce"],
            completed_sequence=  challenge["sequence"],
            frame_timestamps_ms= make_timestamps(),
            entropy=             make_entropy(),
        )
        assert r1.ok is True

        # Second attempt with same nonce
        r2 = await verify_challenge(
            session_id=          "sess-burn",
            nonce=               challenge["nonce"],
            completed_sequence=  challenge["sequence"],
            frame_timestamps_ms= make_timestamps(),
            entropy=             make_entropy(),
        )

    assert r2.ok is False
    assert "not found" in r2.error.lower() or "expired" in r2.error.lower()


@pytest.mark.asyncio
async def test_verify_challenge_unknown_session():
    result = await verify_challenge(
        session_id=          "sess-does-not-exist",
        nonce=               "whatever",
        completed_sequence=  ["blink", "turn_left", "turn_right"],
        frame_timestamps_ms= make_timestamps(),
        entropy=             make_entropy(),
    )
    assert result.ok is False
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_verify_challenge_low_entropy_flagged_not_rejected():
    """Low entropy should flag the session but not hard-reject it."""
    challenge = await generate_challenge("sess-entropy")

    with patch("app.services.liveness_challenge.time") as mock_time:
        mock_time.time.return_value = time.time() + 5.0

        result = await verify_challenge(
            session_id=          "sess-entropy",
            nonce=               challenge["nonce"],
            completed_sequence=  challenge["sequence"],
            frame_timestamps_ms= make_timestamps(),
            entropy=             make_entropy(good=False),
        )

    assert result.ok is True          # NOT rejected
    assert result.flagged is True     # but flagged for review
    assert result.flag_reason != ""


# ── _validate_frame_timing (unit) ─────────────────────────────────────────────

def test_validate_frame_timing_normal():
    ts = make_timestamps(30, delta_ms=33, jitter=5)
    ok, reason = _validate_frame_timing(ts)
    assert ok is True


def test_validate_frame_timing_too_few_frames():
    ok, reason = _validate_frame_timing([1000, 1033, 1066])
    assert ok is False
    assert "few frames" in reason


def test_validate_frame_timing_zero_variance():
    """Perfectly uniform deltas — pre-recorded video signal."""
    base = 1_000_000
    ts = [base + i * 33 for i in range(30)]   # exactly 33ms every frame
    ok, reason = _validate_frame_timing(ts)
    assert ok is False
    assert "variance" in reason


def test_validate_frame_timing_impossible_delta():
    """Frames arriving 1ms apart — physically impossible for a real camera."""
    base = 1_000_000
    ts = [base + i for i in range(30)]   # 1ms deltas
    ok, reason = _validate_frame_timing(ts)
    assert ok is False


# ── _evaluate_entropy (unit) ──────────────────────────────────────────────────

def test_evaluate_entropy_good():
    flagged, reason = _evaluate_entropy(make_entropy(good=True))
    assert flagged is False


def test_evaluate_entropy_bad():
    flagged, reason = _evaluate_entropy(make_entropy(good=False))
    assert flagged is True
    assert reason != ""


def test_evaluate_entropy_single_bad_signal_not_flagged():
    """Only one bad signal should not flag — requires ≥2."""
    entropy = make_entropy(good=True)
    entropy["brightnessVariance"] = 0.1   # only this one is bad
    flagged, _ = _evaluate_entropy(entropy)
    assert flagged is False