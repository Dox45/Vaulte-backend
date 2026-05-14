# """
# liveness_challenge.py
# ─────────────────────
# Server-side challenge lifecycle:
#   1. generate_challenge()  — issue a nonce + random sequence, store in Redis
#   2. verify_challenge()    — validate nonce, sequence order, timing, frame entropy
#   3. burn_challenge()      — delete after use (one-shot nonce)

# Attack vectors mitigated:
#   - Looped / pre-recorded video   → random unpredictable challenge sequence
#   - Replay of a previous session  → nonce burned on first successful verify
#   - Bot automation (too-fast)     → min elapsed time gate
#   - Video replay (frame timing)   → inter-frame delta distribution check
#   - Uniform-light static video    → brightness + noise variance floor
# """

# from __future__ import annotations

# import hashlib
# import hmac
# import logging
# import random
# import secrets
# import time
# from dataclasses import dataclass, field
# from typing import Literal

# from app.core.config import get_settings
# from app.core.redis import get_redis

# logger = logging.getLogger(__name__)
# settings = get_settings()

# # ── Challenge types ────────────────────────────────────────────────────────────
# ChallengeStep = Literal["blink", "turn_left", "turn_right", "nod", "smile"]

# ALL_CHALLENGES: list[ChallengeStep] = [
#     "blink",
#     "turn_left",
#     "turn_right",
#     "nod",
#     "smile",
# ]

# SEQUENCE_LENGTH = 3          # how many steps per session
# SESSION_TTL     = 90         # seconds before challenge expires
# MIN_ELAPSED     = 3.0        # seconds — faster = bot
# MAX_ELAPSED     = SESSION_TTL

# # Frame timing (ms between frames at ~30 fps → 33ms ideal)
# MIN_FRAME_DELTA_MS =  10
# MAX_FRAME_DELTA_MS = 200

# # Entropy thresholds (empirically tuned; flag but don't hard-reject)
# MIN_BRIGHTNESS_VARIANCE = 0.4   # pre-recorded video is unnaturally stable
# MIN_NOISE_VARIANCE      = 0.2

# REDIS_KEY_PREFIX = "liveness:challenge:"


# @dataclass
# class LivenessChallenge:
#     session_id:  str
#     nonce:       str
#     sequence:    list[ChallengeStep]
#     created_at:  float
#     completed:   list[ChallengeStep] = field(default_factory=list)
#     flagged:     bool = False
#     flag_reason: str  = ""


# @dataclass
# class ChallengeVerifyResult:
#     ok:           bool
#     error:        str | None = None
#     flagged:      bool       = False
#     flag_reason:  str        = ""
#     elapsed_ms:   float      = 0.0
#     entropy_ok:   bool       = True


# # ── Public API ─────────────────────────────────────────────────────────────────

# async def generate_challenge(session_id: str) -> dict:
#     """
#     Create and persist a new liveness challenge.
#     Returns the sequence + nonce to send to the client.
#     """
#     redis = await get_redis()

#     # Prevent re-issuing if an active challenge already exists
#     existing = await redis.get(f"{REDIS_KEY_PREFIX}{session_id}")
#     if existing:
#         import json
#         data = json.loads(existing)
#         # If less than half TTL has elapsed, reuse it (idempotent re-fetch)
#         if time.time() - data["created_at"] < SESSION_TTL / 2:
#             logger.info(f"Reusing existing challenge for session {session_id}")
#             return {"sequence": data["sequence"], "nonce": data["nonce"]}

#     sequence = random.sample(ALL_CHALLENGES, k=SEQUENCE_LENGTH)
#     nonce    = secrets.token_hex(24)

#     challenge = LivenessChallenge(
#         session_id=session_id,
#         nonce=nonce,
#         sequence=sequence,
#         created_at=time.time(),
#     )

#     await _persist(challenge)
#     logger.info(f"Challenge issued | session={session_id} | seq={sequence}")
#     return {"sequence": sequence, "nonce": nonce}


# async def verify_challenge(
#     *,
#     session_id:          str,
#     nonce:               str,
#     completed_sequence:  list[str],
#     frame_timestamps_ms: list[int],
#     entropy:             dict,
# ) -> ChallengeVerifyResult:
#     """
#     Full server-side liveness verification.
#     Call this BEFORE forwarding the selfie to Youverify.
#     """
#     redis    = await get_redis()
#     import json

#     raw = await redis.get(f"{REDIS_KEY_PREFIX}{session_id}")
#     if not raw:
#         return ChallengeVerifyResult(ok=False, error="Challenge session not found or expired")

#     data     = json.loads(raw)
#     elapsed  = time.time() - data["created_at"]

#     # ── 1. Nonce integrity ──────────────────────────────────────────────────
#     if not hmac.compare_digest(nonce, data["nonce"]):
#         logger.warning(f"Nonce mismatch | session={session_id}")
#         await _burn(session_id)
#         return ChallengeVerifyResult(ok=False, error="Invalid session nonce")

#     # ── 2. Expiry ───────────────────────────────────────────────────────────
#     if elapsed > MAX_ELAPSED:
#         await _burn(session_id)
#         return ChallengeVerifyResult(ok=False, error="Challenge session expired — please restart")

#     # ── 3. Minimum elapsed (bot gate) ───────────────────────────────────────
#     if elapsed < MIN_ELAPSED:
#         logger.warning(f"Too fast ({elapsed:.2f}s) | session={session_id}")
#         await _burn(session_id)
#         return ChallengeVerifyResult(ok=False, error="Completed too quickly — suspected automation")

#     # ── 4. Challenge sequence must match server-issued order exactly ─────────
#     expected = data["sequence"]
#     if completed_sequence != expected:
#         logger.warning(
#             f"Sequence mismatch | session={session_id} "
#             f"expected={expected} got={completed_sequence}"
#         )
#         await _burn(session_id)
#         return ChallengeVerifyResult(ok=False, error="Challenge sequence mismatch")

#     # ── 5. Frame timestamp sanity ────────────────────────────────────────────
#     timing_ok, timing_reason = _validate_frame_timing(frame_timestamps_ms)
#     if not timing_ok:
#         logger.warning(f"Frame timing anomaly | session={session_id} | {timing_reason}")
#         await _burn(session_id)
#         return ChallengeVerifyResult(ok=False, error=f"Frame timing anomaly: {timing_reason}")

#     # ── 6. Entropy / passive liveness signals (soft flag, not hard reject) ───
#     flagged, flag_reason = _evaluate_entropy(entropy)
#     if flagged:
#         logger.warning(f"Low entropy flag | session={session_id} | {flag_reason}")

#     # ── 7. Burn nonce (one-shot) ─────────────────────────────────────────────
#     await _burn(session_id)

#     return ChallengeVerifyResult(
#         ok=True,
#         flagged=flagged,
#         flag_reason=flag_reason,
#         elapsed_ms=round(elapsed * 1000, 1),
#         entropy_ok=not flagged,
#     )


# # ── Helpers ────────────────────────────────────────────────────────────────────

# def _validate_frame_timing(timestamps: list[int]) -> tuple[bool, str]:
#     """
#     Checks inter-frame deltas for realism.
#     Pre-recorded videos tend to have perfectly uniform or zero-variance deltas.
#     Real webcam streams have natural jitter.
#     """
#     if len(timestamps) < 6:
#         return False, "Too few frames submitted"

#     deltas = [b - a for a, b in zip(timestamps, timestamps[1:])]

#     # Hard bounds
#     out_of_range = [d for d in deltas if d < MIN_FRAME_DELTA_MS or d > MAX_FRAME_DELTA_MS]
#     if len(out_of_range) > len(deltas) * 0.2:   # >20% out of range
#         return False, f"{len(out_of_range)} frame deltas outside [{MIN_FRAME_DELTA_MS},{MAX_FRAME_DELTA_MS}]ms"

#     # Variance: pre-recorded videos have suspiciously low variance
#     mean  = sum(deltas) / len(deltas)
#     var   = sum((d - mean) ** 2 for d in deltas) / len(deltas)
#     if var < 1.0:
#         return False, f"Frame delta variance too low ({var:.3f}) — suspected video replay"

#     return True, ""


# def _evaluate_entropy(entropy: dict) -> tuple[bool, str]:
#     """
#     Soft passive liveness check based on pixel-level entropy signals.
#     Returns (flagged, reason). Flagged sessions are logged for review
#     but NOT hard-rejected here — Youverify face match is the hard gate.
#     """
#     bv = entropy.get("brightnessVariance", 999)
#     nv = entropy.get("noiseVariance", 999)
#     hv = entropy.get("headStabilityVariance", 999)
#     em = entropy.get("earMicroVariance", 999)

#     reasons = []
#     if bv < MIN_BRIGHTNESS_VARIANCE:
#         reasons.append(f"low brightness variance ({bv:.3f})")
#     if nv < MIN_NOISE_VARIANCE:
#         reasons.append(f"low noise variance ({nv:.3f})")
#     if hv < 0.00005:
#         reasons.append(f"unnaturally stable head ({hv:.6f})")
#     if em < 0.0001:
#         reasons.append(f"no EAR micro-movement ({em:.6f})")

#     # Need ≥2 signals to flag (reduces false positives from good lighting)
#     if len(reasons) >= 2:
#         return True, "; ".join(reasons)

#     return False, ""


# async def _persist(challenge: LivenessChallenge) -> None:
#     import json
#     redis = await get_redis()
#     await redis.set(
#         f"{REDIS_KEY_PREFIX}{challenge.session_id}",
#         json.dumps({
#             "nonce":      challenge.nonce,
#             "sequence":   challenge.sequence,
#             "created_at": challenge.created_at,
#         }),
#         ex=SESSION_TTL,
#     )


# async def _burn(session_id: str) -> None:
#     """Delete challenge — makes nonce one-shot."""
#     redis = await get_redis()
#     await redis.delete(f"{REDIS_KEY_PREFIX}{session_id}")
#     logger.debug(f"Nonce burned | session={session_id}")


# async def store_selfie(session_id: str, image_b64: str) -> None:
#     """Store the captured selfie in Redis for later identity verification."""
#     redis = await get_redis()
#     # Store for slightly longer than the challenge (e.g. 10 mins) to allow form filling
#     await redis.set(f"liveness:selfie:{session_id}", image_b64, ex=600)
#     logger.info(f"Selfie stored | session={session_id}")


# async def get_selfie(session_id: str) -> str | None:
#     """Retrieve the stored selfie for this session."""
#     redis = await get_redis()
#     return await redis.get(f"liveness:selfie:{session_id}")



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


# ── Error codes ────────────────────────────────────────────────────────────────
# Each maps to a specific failure mode for structured client/log handling.

class LivenessErrorCode:
    SESSION_NOT_FOUND    = "SESSION_NOT_FOUND"       # Redis miss / expired TTL
    NONCE_MISMATCH       = "NONCE_MISMATCH"          # Tampered or wrong nonce
    SESSION_EXPIRED      = "SESSION_EXPIRED"         # Past MAX_ELAPSED
    COMPLETED_TOO_FAST   = "COMPLETED_TOO_FAST"      # Below MIN_ELAPSED (bot)
    SEQUENCE_MISMATCH    = "SEQUENCE_MISMATCH"       # Steps in wrong order / missing
    FRAME_TIMING_ANOMALY = "FRAME_TIMING_ANOMALY"    # Too few frames / uniform deltas
    LOW_ENTROPY          = "LOW_ENTROPY"             # Passive liveness signals weak (soft)


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
class VerificationScores:
    """
    Per-check scores in [0.0, 1.0]. 1.0 = fully passing, 0.0 = hard fail.
    Composite is a weighted mean of all checks.
    Exposed to the client so UI can surface actionable feedback
    (e.g. "head movement too small" rather than generic "liveness failed").
    """
    sequence_match:   float = 0.0   # 1.0 if order correct, 0.0 otherwise
    timing_score:     float = 0.0   # based on elapsed vs expected window
    frame_quality:    float = 0.0   # frame delta variance normalised
    brightness:       float = 0.0   # brightness variance vs floor
    noise:            float = 0.0   # pixel noise variance vs floor
    head_stability:   float = 0.0   # penalises unnaturally still head
    ear_movement:     float = 0.0   # eye-blink micro-movement score

    @property
    def composite(self) -> float:
        """Weighted composite. Sequence + timing are hard gates so weighted highest."""
        weights = {
            "sequence_match": 0.30,
            "timing_score":   0.20,
            "frame_quality":  0.20,
            "brightness":     0.10,
            "noise":          0.10,
            "head_stability": 0.05,
            "ear_movement":   0.05,
        }
        return round(
            sum(getattr(self, k) * w for k, w in weights.items()),
            3,
        )

    def as_dict(self) -> dict:
        return {
            "sequence_match":  self.sequence_match,
            "timing_score":    self.timing_score,
            "frame_quality":   self.frame_quality,
            "brightness":      self.brightness,
            "noise":           self.noise,
            "head_stability":  self.head_stability,
            "ear_movement":    self.ear_movement,
            "composite":       self.composite,
        }


@dataclass
class ChallengeVerifyResult:
    ok:           bool
    error:        str | None        = None
    error_code:   str | None        = None   # one of LivenessErrorCode.*
    flagged:      bool              = False
    flag_reason:  str               = ""
    elapsed_ms:   float             = 0.0
    entropy_ok:   bool              = True
    scores:       VerificationScores = field(default_factory=VerificationScores)


# ── Public API ─────────────────────────────────────────────────────────────────

async def generate_challenge(session_id: str) -> dict:
    """
    Create and persist a new liveness challenge.
    Returns the sequence + nonce to send to the client.
    """
    redis = await get_redis()

    existing = await redis.get(f"{REDIS_KEY_PREFIX}{session_id}")
    if existing:
        import json
        data = json.loads(existing)
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

    On failure, ChallengeVerifyResult.error_code is one of LivenessErrorCode.*
    and ChallengeVerifyResult.scores carries per-dimension scores so the
    caller can surface actionable feedback to the user.
    """
    redis    = await get_redis()
    import json

    raw = await redis.get(f"{REDIS_KEY_PREFIX}{session_id}")
    if not raw:
        logger.warning(f"Session not found | session={session_id}")
        return ChallengeVerifyResult(
            ok=False,
            error="Challenge session not found or expired — please request a new challenge",
            error_code=LivenessErrorCode.SESSION_NOT_FOUND,
        )

    data    = json.loads(raw)
    elapsed = time.time() - data["created_at"]
    scores  = VerificationScores()

    # ── 1. Nonce integrity ──────────────────────────────────────────────────
    if not hmac.compare_digest(nonce, data["nonce"]):
        logger.warning(f"Nonce mismatch | session={session_id}")
        await _burn(session_id)
        return ChallengeVerifyResult(
            ok=False,
            error="Session token is invalid — please restart the liveness check",
            error_code=LivenessErrorCode.NONCE_MISMATCH,
            scores=scores,
        )

    # ── 2. Expiry ───────────────────────────────────────────────────────────
    if elapsed > MAX_ELAPSED:
        await _burn(session_id)
        return ChallengeVerifyResult(
            ok=False,
            error=f"Challenge expired after {int(elapsed)}s — please request a new one",
            error_code=LivenessErrorCode.SESSION_EXPIRED,
            scores=scores,
        )

    # ── 3. Minimum elapsed (bot gate) ───────────────────────────────────────
    if elapsed < MIN_ELAPSED:
        logger.warning(f"Too fast ({elapsed:.2f}s) | session={session_id}")
        await _burn(session_id)
        return ChallengeVerifyResult(
            ok=False,
            error=(
                f"Completed in {elapsed:.1f}s — too fast to be a real user "
                f"(minimum {MIN_ELAPSED}s required)"
            ),
            error_code=LivenessErrorCode.COMPLETED_TOO_FAST,
            scores=scores,
        )

    # Timing score: 1.0 in the comfortable window, degrades at extremes
    scores.timing_score = _score_elapsed(elapsed)

    # ── 4. Challenge sequence must match server-issued order exactly ─────────
    expected = data["sequence"]
    if completed_sequence != expected:
        missing  = [s for s in expected if s not in completed_sequence]
        wrong_order = completed_sequence != expected and set(completed_sequence) == set(expected)

        if wrong_order:
            detail = f"Steps were performed in the wrong order — expected: {expected}"
        elif missing:
            detail = f"Missing steps: {missing} — complete all {SEQUENCE_LENGTH} actions"
        else:
            detail = f"Unexpected steps submitted — expected: {expected}"

        logger.warning(
            f"Sequence mismatch | session={session_id} "
            f"expected={expected} got={completed_sequence}"
        )
        scores.sequence_match = 0.0
        await _burn(session_id)
        return ChallengeVerifyResult(
            ok=False,
            error=detail,
            error_code=LivenessErrorCode.SEQUENCE_MISMATCH,
            scores=scores,
        )

    scores.sequence_match = 1.0

    # ── 5. Frame timestamp sanity ────────────────────────────────────────────
    timing_ok, timing_reason, frame_score = _validate_frame_timing(frame_timestamps_ms)
    scores.frame_quality = frame_score
    if not timing_ok:
        logger.warning(f"Frame timing anomaly | session={session_id} | {timing_reason}")
        await _burn(session_id)
        return ChallengeVerifyResult(
            ok=False,
            error=f"Video quality check failed: {timing_reason}",
            error_code=LivenessErrorCode.FRAME_TIMING_ANOMALY,
            scores=scores,
        )

    # ── 6. Entropy / passive liveness signals (soft flag) ───────────────────
    flagged, flag_reason, entropy_scores = _evaluate_entropy(entropy)
    scores.brightness     = entropy_scores["brightness"]
    scores.noise          = entropy_scores["noise"]
    scores.head_stability = entropy_scores["head_stability"]
    scores.ear_movement   = entropy_scores["ear_movement"]

    if flagged:
        logger.warning(f"Low entropy flag | session={session_id} | {flag_reason}")

    # ── 7. Burn nonce (one-shot) ─────────────────────────────────────────────
    await _burn(session_id)

    logger.info(
        f"Liveness verified | session={session_id} "
        f"| composite_score={scores.composite} | flagged={flagged} "
        f"| elapsed={elapsed:.2f}s"
    )

    return ChallengeVerifyResult(
        ok=True,
        flagged=flagged,
        flag_reason=flag_reason,
        elapsed_ms=round(elapsed * 1000, 1),
        entropy_ok=not flagged,
        scores=scores,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _score_elapsed(elapsed: float) -> float:
    """
    Returns 1.0 for the comfortable middle window, grades down toward the edges.
    < MIN_ELAPSED → 0.0 (already rejected before this is called)
    MIN_ELAPSED–10s → ramps 0.5→1.0
    10s–60s → 1.0 (sweet spot)
    60s–MAX_ELAPSED → ramps 1.0→0.5
    """
    if elapsed < MIN_ELAPSED:
        return 0.0
    if elapsed <= 10:
        return round(0.5 + 0.5 * (elapsed - MIN_ELAPSED) / (10 - MIN_ELAPSED), 3)
    if elapsed <= 60:
        return 1.0
    return round(max(0.5, 1.0 - (elapsed - 60) / (MAX_ELAPSED - 60) * 0.5), 3)


def _validate_frame_timing(timestamps: list[int]) -> tuple[bool, str, float]:
    """
    Checks inter-frame deltas for realism.
    Returns (ok, reason, score_0_to_1).
    Pre-recorded videos tend to have perfectly uniform or zero-variance deltas.
    """
    if len(timestamps) < 6:
        return False, f"Only {len(timestamps)} frames received — at least 6 required for analysis", 0.0

    deltas = [b - a for a, b in zip(timestamps, timestamps[1:])]

    out_of_range = [d for d in deltas if d < MIN_FRAME_DELTA_MS or d > MAX_FRAME_DELTA_MS]
    bad_ratio    = len(out_of_range) / len(deltas)
    if bad_ratio > 0.2:
        return (
            False,
            f"{len(out_of_range)}/{len(deltas)} frame gaps outside "
            f"[{MIN_FRAME_DELTA_MS}–{MAX_FRAME_DELTA_MS}ms] — "
            "check camera frame rate or network buffering",
            round(1.0 - bad_ratio, 3),
        )

    mean = sum(deltas) / len(deltas)
    var  = sum((d - mean) ** 2 for d in deltas) / len(deltas)
    if var < 1.0:
        return (
            False,
            f"Frame timing is unnaturally uniform (variance={var:.3f}) — "
            "suspected video replay or screen recording",
            0.0,
        )

    # Normalise variance to a score: higher variance (up to ~150) = better
    score = round(min(1.0, var / 150.0), 3)
    return True, "", score


def _evaluate_entropy(entropy: dict) -> tuple[bool, str, dict[str, float]]:
    """
    Soft passive liveness check.
    Returns (flagged, reason, per_signal_scores).
    Flagged sessions are logged for review but NOT hard-rejected —
    Youverify face match is the hard gate.
    """
    bv = entropy.get("brightnessVariance", 999)
    nv = entropy.get("noiseVariance", 999)
    hv = entropy.get("headStabilityVariance", 999)
    em = entropy.get("earMicroVariance", 999)

    # Score each signal: 0.0 = well below floor, 1.0 = at or above floor
    scores = {
        "brightness":     min(1.0, bv / MIN_BRIGHTNESS_VARIANCE),
        "noise":          min(1.0, nv / MIN_NOISE_VARIANCE),
        "head_stability": min(1.0, hv / 0.00005) if hv < 1.0 else 1.0,
        "ear_movement":   min(1.0, em / 0.0001)  if em < 1.0 else 1.0,
    }

    reasons = []
    if bv < MIN_BRIGHTNESS_VARIANCE:
        reasons.append(
            f"lighting appears too uniform (variance={bv:.3f}) — "
            "try a naturally lit environment"
        )
    if nv < MIN_NOISE_VARIANCE:
        reasons.append(
            f"image noise is suspiciously low (variance={nv:.3f}) — "
            "suspected static image or screen recording"
        )
    if hv < 0.00005:
        reasons.append(
            f"head appears completely still (stability={hv:.6f}) — "
            "natural micro-movement expected during liveness check"
        )
    if em < 0.0001:
        reasons.append(
            f"no eye micro-movement detected (EAR={em:.6f}) — "
            "ensure eyes are fully visible and blink naturally"
        )

    flagged = len(reasons) >= 2
    return flagged, "; ".join(reasons), scores


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
    redis = await get_redis()
    await redis.delete(f"{REDIS_KEY_PREFIX}{session_id}")
    logger.debug(f"Nonce burned | session={session_id}")


async def store_selfie(session_id: str, image_b64: str) -> None:
    redis = await get_redis()
    await redis.set(f"liveness:selfie:{session_id}", image_b64, ex=600)
    logger.info(f"Selfie stored | session={session_id}")


async def get_selfie(session_id: str) -> str | None:
    redis = await get_redis()
    return await redis.get(f"liveness:selfie:{session_id}")