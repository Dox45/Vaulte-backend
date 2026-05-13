"""
liveness_challenge.py
─────────────────────
Server-side challenge lifecycle:
  1. generate_challenge()  — issue a nonce + random sequence, store in Redis
  2. verify_challenge()    — validate nonce, sequence order, timing, frame entropy
  3. burn_challenge()      — delete after use (one-shot nonce)

Attack vectors mitigated:
  - Looped / pre-recorded video   → random unpredictable challenge sequence
  - Replay of a previous session  → nonce burned on first successful verify
  - Bot automation (too-fast)     → min elapsed time gate
  - Video replay (frame timing)   → inter-frame delta distribution check
  - Uniform-light static video    → brightness + noise variance floor
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Literal

from app.core.config import get_settings
from app.core.redis import get_redis

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Challenge types ────────────────────────────────────────────────────────────
ChallengeStep = Literal["blink", "turn_left", "turn_right", "nod", "smile"]

ALL_CHALLENGES: list[ChallengeStep] = [
    "blink",
    "turn_left",
    "turn_right",
    "nod",
    "smile",
]

SEQUENCE_LENGTH = 3          # how many steps per session
SESSION_TTL     = 90         # seconds before challenge expires
MIN_ELAPSED     = 3.0        # seconds — faster = bot
MAX_ELAPSED     = SESSION_TTL

# Frame timing (ms between frames at ~30 fps → 33ms ideal)
MIN_FRAME_DELTA_MS =  10
MAX_FRAME_DELTA_MS = 200

# Entropy thresholds (empirically tuned; flag but don't hard-reject)
MIN_BRIGHTNESS_VARIANCE = 0.4   # pre-recorded video is unnaturally stable
MIN_NOISE_VARIANCE      = 0.2

REDIS_KEY_PREFIX = "liveness:challenge:"


@dataclass
class LivenessChallenge:
    session_id:  str
    nonce:       str
    sequence:    list[ChallengeStep]
    created_at:  float
    completed:   list[ChallengeStep] = field(default_factory=list)
    flagged:     bool = False
    flag_reason: str  = ""


@dataclass
class ChallengeVerifyResult:
    ok:           bool
    error:        str | None = None
    flagged:      bool       = False
    flag_reason:  str        = ""
    elapsed_ms:   float      = 0.0
    entropy_ok:   bool       = True


# ── Public API ─────────────────────────────────────────────────────────────────

async def generate_challenge(session_id: str) -> dict:
    """
    Create and persist a new liveness challenge.
    Returns the sequence + nonce to send to the client.
    """
    redis = await get_redis()

    # Prevent re-issuing if an active challenge already exists
    existing = await redis.get(f"{REDIS_KEY_PREFIX}{session_id}")
    if existing:
        import json
        data = json.loads(existing)
        # If less than half TTL has elapsed, reuse it (idempotent re-fetch)
        if time.time() - data["created_at"] < SESSION_TTL / 2:
            logger.info(f"Reusing existing challenge for session {session_id}")
            return {"sequence": data["sequence"], "nonce": data["nonce"]}

    sequence = random.sample(ALL_CHALLENGES, k=SEQUENCE_LENGTH)
    nonce    = secrets.token_hex(24)

    challenge = LivenessChallenge(
        session_id=session_id,
        nonce=nonce,
        sequence=sequence,
        created_at=time.time(),
    )

    await _persist(challenge)
    logger.info(f"Challenge issued | session={session_id} | seq={sequence}")
    return {"sequence": sequence, "nonce": nonce}


async def verify_challenge(
    *,
    session_id:          str,
    nonce:               str,
    completed_sequence:  list[str],
    frame_timestamps_ms: list[int],
    entropy:             dict,
) -> ChallengeVerifyResult:
    """
    Full server-side liveness verification.
    Call this BEFORE forwarding the selfie to Youverify.
    """
    redis    = await get_redis()
    import json

    raw = await redis.get(f"{REDIS_KEY_PREFIX}{session_id}")
    if not raw:
        return ChallengeVerifyResult(ok=False, error="Challenge session not found or expired")

    data     = json.loads(raw)
    elapsed  = time.time() - data["created_at"]

    # ── 1. Nonce integrity ──────────────────────────────────────────────────
    if not hmac.compare_digest(nonce, data["nonce"]):
        logger.warning(f"Nonce mismatch | session={session_id}")
        await _burn(session_id)
        return ChallengeVerifyResult(ok=False, error="Invalid session nonce")

    # ── 2. Expiry ───────────────────────────────────────────────────────────
    if elapsed > MAX_ELAPSED:
        await _burn(session_id)
        return ChallengeVerifyResult(ok=False, error="Challenge session expired — please restart")

    # ── 3. Minimum elapsed (bot gate) ───────────────────────────────────────
    if elapsed < MIN_ELAPSED:
        logger.warning(f"Too fast ({elapsed:.2f}s) | session={session_id}")
        await _burn(session_id)
        return ChallengeVerifyResult(ok=False, error="Completed too quickly — suspected automation")

    # ── 4. Challenge sequence must match server-issued order exactly ─────────
    expected = data["sequence"]
    if completed_sequence != expected:
        logger.warning(
            f"Sequence mismatch | session={session_id} "
            f"expected={expected} got={completed_sequence}"
        )
        await _burn(session_id)
        return ChallengeVerifyResult(ok=False, error="Challenge sequence mismatch")

    # ── 5. Frame timestamp sanity ────────────────────────────────────────────
    timing_ok, timing_reason = _validate_frame_timing(frame_timestamps_ms)
    if not timing_ok:
        logger.warning(f"Frame timing anomaly | session={session_id} | {timing_reason}")
        await _burn(session_id)
        return ChallengeVerifyResult(ok=False, error=f"Frame timing anomaly: {timing_reason}")

    # ── 6. Entropy / passive liveness signals (soft flag, not hard reject) ───
    flagged, flag_reason = _evaluate_entropy(entropy)
    if flagged:
        logger.warning(f"Low entropy flag | session={session_id} | {flag_reason}")

    # ── 7. Burn nonce (one-shot) ─────────────────────────────────────────────
    await _burn(session_id)

    return ChallengeVerifyResult(
        ok=True,
        flagged=flagged,
        flag_reason=flag_reason,
        elapsed_ms=round(elapsed * 1000, 1),
        entropy_ok=not flagged,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _validate_frame_timing(timestamps: list[int]) -> tuple[bool, str]:
    """
    Checks inter-frame deltas for realism.
    Pre-recorded videos tend to have perfectly uniform or zero-variance deltas.
    Real webcam streams have natural jitter.
    """
    if len(timestamps) < 6:
        return False, "Too few frames submitted"

    deltas = [b - a for a, b in zip(timestamps, timestamps[1:])]

    # Hard bounds
    out_of_range = [d for d in deltas if d < MIN_FRAME_DELTA_MS or d > MAX_FRAME_DELTA_MS]
    if len(out_of_range) > len(deltas) * 0.2:   # >20% out of range
        return False, f"{len(out_of_range)} frame deltas outside [{MIN_FRAME_DELTA_MS},{MAX_FRAME_DELTA_MS}]ms"

    # Variance: pre-recorded videos have suspiciously low variance
    mean  = sum(deltas) / len(deltas)
    var   = sum((d - mean) ** 2 for d in deltas) / len(deltas)
    if var < 1.0:
        return False, f"Frame delta variance too low ({var:.3f}) — suspected video replay"

    return True, ""


def _evaluate_entropy(entropy: dict) -> tuple[bool, str]:
    """
    Soft passive liveness check based on pixel-level entropy signals.
    Returns (flagged, reason). Flagged sessions are logged for review
    but NOT hard-rejected here — Youverify face match is the hard gate.
    """
    bv = entropy.get("brightnessVariance", 999)
    nv = entropy.get("noiseVariance", 999)
    hv = entropy.get("headStabilityVariance", 999)
    em = entropy.get("earMicroVariance", 999)

    reasons = []
    if bv < MIN_BRIGHTNESS_VARIANCE:
        reasons.append(f"low brightness variance ({bv:.3f})")
    if nv < MIN_NOISE_VARIANCE:
        reasons.append(f"low noise variance ({nv:.3f})")
    if hv < 0.00005:
        reasons.append(f"unnaturally stable head ({hv:.6f})")
    if em < 0.0001:
        reasons.append(f"no EAR micro-movement ({em:.6f})")

    # Need ≥2 signals to flag (reduces false positives from good lighting)
    if len(reasons) >= 2:
        return True, "; ".join(reasons)

    return False, ""


async def _persist(challenge: LivenessChallenge) -> None:
    import json
    redis = await get_redis()
    await redis.set(
        f"{REDIS_KEY_PREFIX}{challenge.session_id}",
        json.dumps({
            "nonce":      challenge.nonce,
            "sequence":   challenge.sequence,
            "created_at": challenge.created_at,
        }),
        ex=SESSION_TTL,
    )


async def _burn(session_id: str) -> None:
    """Delete challenge — makes nonce one-shot."""
    redis = await get_redis()
    await redis.delete(f"{REDIS_KEY_PREFIX}{session_id}")
    logger.debug(f"Nonce burned | session={session_id}")


async def store_selfie(session_id: str, image_b64: str) -> None:
    """Store the captured selfie in Redis for later identity verification."""
    redis = await get_redis()
    # Store for slightly longer than the challenge (e.g. 10 mins) to allow form filling
    await redis.set(f"liveness:selfie:{session_id}", image_b64, ex=600)
    logger.info(f"Selfie stored | session={session_id}")


async def get_selfie(session_id: str) -> str | None:
    """Retrieve the stored selfie for this session."""
    redis = await get_redis()
    return await redis.get(f"liveness:selfie:{session_id}")