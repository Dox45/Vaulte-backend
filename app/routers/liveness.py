"""
routes/liveness.py
──────────────────
Liveness verification endpoints.

Flow:
  GET  /vendor/liveness/challenge   → issue nonce + random sequence
  POST /vendor/liveness             → verify challenge then call Youverify

The old POST /vendor/liveness accepted blink_detected + head_turn_detected
as booleans from the client — trivially spoofable. The new contract:
  - Client must first fetch a challenge (server picks the sequence)
  - Client submits nonce + completed steps in server-issued order
  - Server validates timing, entropy, and sequence before touching Youverify
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings
from app.core.deps import get_current_vendor_session
from app.services.liveness_challenge import (
    ChallengeStep,
    generate_challenge,
    verify_challenge,
    store_selfie,
)

logger   = logging.getLogger(__name__)
settings = get_settings()
router   = APIRouter(prefix="/vendor/liveness", tags=["liveness"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class ChallengeResponse(BaseModel):
    sequence: list[ChallengeStep]
    nonce:    str


class FrameEntropy(BaseModel):
    """Passive liveness signals collected by the client."""
    brightnessVariance:    float = Field(ge=0)
    noiseVariance:         float = Field(ge=0)
    headStabilityVariance: float = Field(ge=0)
    earMicroVariance:      float = Field(ge=0)
    blinkLatencyMs:        float = Field(ge=0)
    turnLatencyMs:         float = Field(ge=0)


class LivenessSubmission(BaseModel):
    session_id:          str
    nonce:               str
    frame_base64:        str            # data URI  "data:image/jpeg;base64,..."
    completed_sequence:  list[ChallengeStep]
    frame_timestamps_ms: list[int]      # epoch-ms of each processed frame
    entropy:             FrameEntropy
    # Legacy booleans kept for backward-compat logging only — not trusted
    blink_detected:      bool = False
    head_turn_detected:  bool = False

    @field_validator("frame_base64")
    @classmethod
    def validate_frame(cls, v: str) -> str:
        if not v.startswith("data:image/"):
            raise ValueError("frame_base64 must be a data URI")
        # rough size check: 400 KB base64 ≈ 300 KB image
        estimated_kb = len(v) * 3 / 4 / 1024
        if estimated_kb > 500:
            raise ValueError(f"Frame too large ({estimated_kb:.0f} KB) — max 500 KB")
        return v

    @field_validator("frame_timestamps_ms")
    @classmethod
    def validate_timestamps(cls, v: list[int]) -> list[int]:
        if len(v) < 6:
            raise ValueError("At least 6 frame timestamps required")
        if len(v) > 500:
            raise ValueError("Too many frame timestamps")
        return v

    @field_validator("completed_sequence")
    @classmethod
    def validate_sequence(cls, v: list[ChallengeStep]) -> list[ChallengeStep]:
        if len(v) != 3:
            raise ValueError("completed_sequence must contain exactly 3 steps")
        return v


class LivenessResult(BaseModel):
    success:          bool
    liveness_passed:  bool
    message:          str
    flagged:          bool = False
    flag_reason:      str  = ""
    elapsed_ms:       float = 0.0


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/challenge", response_model=ChallengeResponse)
async def get_challenge(
    request: Request,
    session=Depends(get_current_vendor_session),
) -> ChallengeResponse:
    """
    Issue a randomised liveness challenge for this session.
    The client MUST complete steps in the returned order.
    Idempotent: re-fetching within half the TTL returns the same challenge.
    """
    session_id = session.id
    result     = await generate_challenge(session_id)
    logger.info(f"Challenge issued | session={session_id} | ip={request.client.host}")
    return ChallengeResponse(**result)


@router.post("", response_model=LivenessResult)
async def submit_liveness(
    payload: LivenessSubmission,
    request: Request,
    session=Depends(get_current_vendor_session),
) -> LivenessResult:
    """
    Full liveness + identity verification pipeline:

    1. Verify server-issued challenge (nonce, sequence, timing, entropy)
    2. Forward selfie to Youverify NIN endpoint
    3. Return composite identity result
    """
    session_id = session.id

    # ── Step 1: challenge verification ───────────────────────────────────────
    verify_result = await verify_challenge(
        session_id=session_id,
        nonce=payload.nonce,
        completed_sequence=list(payload.completed_sequence),
        frame_timestamps_ms=payload.frame_timestamps_ms,
        entropy=payload.entropy.model_dump(),
    )

    if not verify_result.ok:
        logger.warning(
            f"Liveness challenge failed | session={session_id} "
            f"| ip={request.client.host} | reason={verify_result.error}"
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=verify_result.error,
        )

    # ── Step 2: Store selfie for identity verification ───────────────────────
    await store_selfie(session_id, payload.frame_base64)

    logger.info(
        f"Liveness complete | session={session_id} "
        f"| passed=True | flagged={verify_result.flagged} "
        f"| elapsed={verify_result.elapsed_ms}ms"
    )

    return LivenessResult(
        success=True,
        liveness_passed=True,
        message="Liveness verified and selfie captured.",
        flagged=verify_result.flagged,
        flag_reason=verify_result.flag_reason,
        elapsed_ms=verify_result.elapsed_ms,
    )
