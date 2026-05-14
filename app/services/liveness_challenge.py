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

Threshold notes (tuned for real-world webcam conditions):
  - Brightness/noise variance checks are SOFT FLAGS only — modern webcams have
    built-in noise reduction and AGC that naturally produce low variance values.
    These signals alone should never hard-reject a real user.
  - Frame timing variance floor is set conservatively because requestAnimationFrame
    + MediaPipe produce smooth, low-jitter frame streams by design.
  - Hard rejections are reserved for: wrong sequence, bad nonce, bot-speed timing,
    and critically low frame counts. Everything else is a soft flag for review.
"""

from __future__ import annotations

import hmac
import logging
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Literal

from app.core.config import get_settings
from app.core.redis import get_redis

logger   = logging.getLogger(__name__)
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

SEQUENCE_LENGTH = 3     # steps per session
SESSION_TTL     = 90    # seconds before Redis key expires
MIN_ELAPSED     = 2.5   # seconds — was 3.0; real users on mobile can be quick
MAX_ELAPSED     = SESSION_TTL

# ── Frame timing bounds ────────────────────────────────────────────────────────
# requestAnimationFrame @ 30fps = ~33ms per frame.
# Allow 5ms–500ms to accommodate slow devices, tab visibility changes, and
# the initial MediaPipe warm-up burst that produces back-to-back frames.
MIN_FRAME_DELTA_MS  =   5   # was 10  — allow fast GPU/RAF bursts
MAX_FRAME_DELTA_MS  = 500   # was 200 — allow slow devices and tab-switch pauses

# Only flag if >40% of deltas are out of range (was 20%)
FRAME_DELTA_BAD_RATIO = 0.40

# Variance floor for frame timing.
# RAF+MediaPipe is intentionally smooth — 0.1 is realistic for a real stream.
# Pre-recorded video static loops have variance = 0.0 exactly.
MIN_FRAME_DELTA_VARIANCE = 0.1   # was 1.0 — was rejecting real users

# ── Entropy thresholds (SOFT FLAGS only — never hard-reject on these alone) ───
# These are intentionally lenient. A 32×32 pixel patch from a webcam with AGC
# (automatic gain control) or noise reduction will naturally produce low values.
# We require ALL FOUR signals to be simultaneously below threshold to flag,
# which is the fingerprint of a genuinely static or pre-recorded feed.
MIN_BRIGHTNESS_VARIANCE = 0.05   # was 0.4  — webcam AGC kills variance
MIN_NOISE_VARIANCE      = 0.02   # was 0.2  — noise-reduced cameras read near zero
MIN_HEAD_STABILITY      = 0.00001  # was 0.00005 — most people hold fairly still
MIN_EAR_MOVEMENT        = 0.00002  # was 0.0001  — subtle eye micro-movement

# Require ALL four signals to trip simultaneously for a soft flag.
# This prevents false positives from good cameras or well-lit environments.
ENTROPY_FLAG_THRESHOLD = 4   # was 2 — must fail ALL signals, not just 2

REDIS_KEY_PREFIX = "liveness:challenge:"


# ── Error codes ────────────────────────────────────────────────────────────────
class LivenessErrorCode:
    SESSION_NOT_FOUND    = "SESSION_NOT_FOUND"
    NONCE_MISMATCH       = "NONCE_MISMATCH"
    SESSION_EXPIRED      = "SESSION_EXPIRED"
    COMPLETED_TOO_FAST   = "COMPLETED_TOO_FAST"
    SEQUENCE_MISMATCH    = "SEQUENCE_MISMATCH"
    FRAME_TIMING_ANOMALY = "FRAME_TIMING_ANOMALY"
    LOW_ENTROPY          = "LOW_ENTROPY"


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
    """
    sequence_match: float = 0.0
    timing_score:   float = 0.0
    frame_quality:  float = 0.0
    brightness:     float = 0.0
    noise:          float = 0.0
    head_stability: float = 0.0
    ear_movement:   float = 0.0

    @property
    def composite(self) -> float:
        weights = {
            "sequence_match": 0.30,
            "timing_score":   0.20,
            "frame_quality":  0.20,
            "brightness":     0.10,
            "noise":          0.10,
            "head_stability": 0.05,
            "ear_movement":   0.05,
        }
        return round(sum(getattr(self, k) * w for k, w in weights.items()), 3)

    def as_dict(self) -> dict:
        return {
            "sequence_match": self.sequence_match,
            "timing_score":   self.timing_score,
            "frame_quality":  self.frame_quality,
            "brightness":     self.brightness,
            "noise":          self.noise,
            "head_stability": self.head_stability,
            "ear_movement":   self.ear_movement,
            "composite":      self.composite,
        }


@dataclass
class ChallengeVerifyResult:
    ok:          bool
    error:       str | None          = None
    error_code:  str | None          = None
    flagged:     bool                = False
    flag_reason: str                 = ""
    elapsed_ms:  float               = 0.0
    entropy_ok:  bool                = True
    scores:      VerificationScores  = field(default_factory=VerificationScores)


# ── Public API ─────────────────────────────────────────────────────────────────

async def generate_challenge(session_id: str) -> dict:
    """
    Create and persist a new liveness challenge.
    Returns the sequence + nonce to send to the client.
    Idempotent: re-fetching within half the TTL reuses the existing challenge.
    """
    import json
    redis = await get_redis()

    existing = await redis.get(f"{REDIS_KEY_PREFIX}{session_id}")
    if existing:
        data = json.loads(existing)
        if time.time() - data["created_at"] < SESSION_TTL / 2:
            logger.info(f"Reusing existing challenge | session={session_id}")
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

    Hard rejections (return ok=False):
      - Session missing / expired
      - Bad nonce
      - Completed in under MIN_ELAPSED seconds
      - Wrong challenge sequence
      - Frame count below minimum

    Soft flags (ok=True, flagged=True):
      - ALL four entropy signals simultaneously below threshold
      - Frame timing too uniform (only if variance is exactly 0 — static feed)

    Everything else passes. The Youverify face match is the hard identity gate.
    """
    import json
    redis   = await get_redis()
    scores  = VerificationScores()

    # ── 1. Session lookup ───────────────────────────────────────────────────
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

    # ── 2. Nonce integrity ──────────────────────────────────────────────────
    if not hmac.compare_digest(nonce, data["nonce"]):
        logger.warning(f"Nonce mismatch | session={session_id}")
        await _burn(session_id)
        return ChallengeVerifyResult(
            ok=False,
            error="Session token is invalid — please restart the liveness check",
            error_code=LivenessErrorCode.NONCE_MISMATCH,
            scores=scores,
        )

    # ── 3. Expiry ───────────────────────────────────────────────────────────
    if elapsed > MAX_ELAPSED:
        await _burn(session_id)
        return ChallengeVerifyResult(
            ok=False,
            error=f"Challenge expired after {int(elapsed)}s — please request a new one",
            error_code=LivenessErrorCode.SESSION_EXPIRED,
            scores=scores,
        )

    # ── 4. Bot-speed gate ───────────────────────────────────────────────────
    if elapsed < MIN_ELAPSED:
        logger.warning(f"Completed too fast ({elapsed:.2f}s) | session={session_id}")
        await _burn(session_id)
        return ChallengeVerifyResult(
            ok=False,
            error=(
                f"Completed in {elapsed:.1f}s — please follow each step naturally "
                f"(minimum {MIN_ELAPSED}s required)"
            ),
            error_code=LivenessErrorCode.COMPLETED_TOO_FAST,
            scores=scores,
        )

    scores.timing_score = _score_elapsed(elapsed)

    # ── 5. Challenge sequence ───────────────────────────────────────────────
    expected = data["sequence"]
    if completed_sequence != expected:
        missing     = [s for s in expected if s not in completed_sequence]
        wrong_order = set(completed_sequence) == set(expected)

        if wrong_order:
            detail = f"Steps performed in the wrong order — expected: {expected}"
        elif missing:
            detail = f"Missing steps: {missing} — complete all {SEQUENCE_LENGTH} actions"
        else:
            detail = f"Unexpected steps submitted — expected: {expected}"

        logger.warning(
            f"Sequence mismatch | session={session_id} "
            f"| expected={expected} | got={completed_sequence}"
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

    # ── 6. Frame count + timing (lenient) ───────────────────────────────────
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

    # ── 7. Entropy soft flags ────────────────────────────────────────────────
    flagged, flag_reason, entropy_scores = _evaluate_entropy(entropy)
    scores.brightness     = entropy_scores["brightness"]
    scores.noise          = entropy_scores["noise"]
    scores.head_stability = entropy_scores["head_stability"]
    scores.ear_movement   = entropy_scores["ear_movement"]

    if flagged:
        logger.warning(
            f"Entropy flag (soft) | session={session_id} | {flag_reason} "
            f"| composite={scores.composite}"
        )

    # ── 8. Burn nonce ────────────────────────────────────────────────────────
    await _burn(session_id)

    logger.info(
        f"Liveness verified | session={session_id} "
        f"| composite={scores.composite} | flagged={flagged} | elapsed={elapsed:.2f}s"
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
    """1.0 in the comfortable 2.5–60s window, grades down at the extremes."""
    if elapsed < MIN_ELAPSED:
        return 0.0
    if elapsed <= 10:
        return round(0.5 + 0.5 * (elapsed - MIN_ELAPSED) / (10 - MIN_ELAPSED), 3)
    if elapsed <= 60:
        return 1.0
    return round(max(0.5, 1.0 - (elapsed - 60) / (MAX_ELAPSED - 60) * 0.5), 3)


def _validate_frame_timing(timestamps: list[int]) -> tuple[bool, str, float]:
    """
    Lenient frame timing check.

    Hard rejects only:
      - Fewer than 6 frames (can't do any analysis)
      - More than FRAME_DELTA_BAD_RATIO of deltas outside bounds
      - Variance exactly 0 (static image / perfectly looped video)

    Everything else scores proportionally and passes.
    """
    if len(timestamps) < 6:
        return (
            False,
            f"Only {len(timestamps)} frames received — at least 6 required",
            0.0,
        )

    deltas = [b - a for a, b in zip(timestamps, timestamps[1:])]

    # Out-of-range ratio check
    out_of_range = [d for d in deltas if d < MIN_FRAME_DELTA_MS or d > MAX_FRAME_DELTA_MS]
    bad_ratio    = len(out_of_range) / len(deltas)
    if bad_ratio > FRAME_DELTA_BAD_RATIO:
        return (
            False,
            (
                f"{len(out_of_range)}/{len(deltas)} frame gaps outside "
                f"[{MIN_FRAME_DELTA_MS}–{MAX_FRAME_DELTA_MS}ms] — "
                "check camera or browser tab visibility"
            ),
            round(1.0 - bad_ratio, 3),
        )

    # Variance check — only hard-reject exactly-zero variance (static loop)
    mean = sum(deltas) / len(deltas)
    var  = sum((d - mean) ** 2 for d in deltas) / len(deltas)
    if var < MIN_FRAME_DELTA_VARIANCE:
        return (
            False,
            (
                f"Frame timing is perfectly uniform (variance={var:.4f}) — "
                "suspected static image or looped video"
            ),
            0.0,
        )

    # Score: variance up to ~300 = 1.0 (generous ceiling for slow devices)
    score = round(min(1.0, var / 300.0), 3)
    return True, "", score


def _evaluate_entropy(entropy: dict) -> tuple[bool, str, dict[str, float]]:
    """
    Soft passive liveness check. NEVER hard-rejects.

    Flags only when ALL four signals are simultaneously below their (very low)
    thresholds — the signature of a genuinely static or pre-recorded feed.
    A good camera in a well-lit room failing 1–2 signals is completely normal.
    """
    bv = entropy.get("brightnessVariance",    999)
    nv = entropy.get("noiseVariance",         999)
    hv = entropy.get("headStabilityVariance", 999)
    em = entropy.get("earMicroVariance",      999)

    # Score each signal against its (relaxed) floor
    scores = {
        "brightness":     min(1.0, bv / MIN_BRIGHTNESS_VARIANCE) if bv < MIN_BRIGHTNESS_VARIANCE * 10 else 1.0,
        "noise":          min(1.0, nv / MIN_NOISE_VARIANCE)       if nv < MIN_NOISE_VARIANCE * 10 else 1.0,
        "head_stability": min(1.0, hv / MIN_HEAD_STABILITY)       if hv < 1.0 else 1.0,
        "ear_movement":   min(1.0, em / MIN_EAR_MOVEMENT)         if em < 1.0 else 1.0,
    }

    reasons: list[str] = []

    if bv < MIN_BRIGHTNESS_VARIANCE:
        reasons.append(
            f"brightness variance very low ({bv:.4f}) — "
            "may indicate static image or extreme low light"
        )
    if nv < MIN_NOISE_VARIANCE:
        reasons.append(
            f"noise variance very low ({nv:.4f}) — "
            "may indicate a noise-free synthetic source"
        )
    if hv < MIN_HEAD_STABILITY:
        reasons.append(
            f"no head micro-movement detected ({hv:.6f}) — "
            "head appears completely locked"
        )
    if em < MIN_EAR_MOVEMENT:
        reasons.append(
            f"no eye micro-movement detected ({em:.6f}) — "
            "eyes appear completely static"
        )

    # Flag only if ALL four signals trip simultaneously
    flagged = len(reasons) >= ENTROPY_FLAG_THRESHOLD
    return flagged, "; ".join(reasons), scores


# ── Redis helpers ──────────────────────────────────────────────────────────────

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