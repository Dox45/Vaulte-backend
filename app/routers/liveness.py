# """
# routes/liveness.py
# ──────────────────
# Liveness verification endpoints.

# Flow:
#   GET  /vendor/liveness/challenge   → issue nonce + random sequence
#   POST /vendor/liveness             → verify challenge then call Youverify

# The old POST /vendor/liveness accepted blink_detected + head_turn_detected
# as booleans from the client — trivially spoofable. The new contract:
#   - Client must first fetch a challenge (server picks the sequence)
#   - Client submits nonce + completed steps in server-issued order
#   - Server validates timing, entropy, and sequence before touching Youverify
# """

# from __future__ import annotations

# import logging
# from typing import Annotated

# from fastapi import APIRouter, Depends, HTTPException, Request, status
# from pydantic import BaseModel, Field, field_validator

# from app.core.config import get_settings
# from app.core.deps import get_current_vendor_session
# from app.services.liveness_challenge import (
#     ChallengeStep,
#     generate_challenge,
#     verify_challenge,
#     store_selfie,
# )

# logger   = logging.getLogger(__name__)
# settings = get_settings()
# router   = APIRouter(prefix="/vendor/liveness", tags=["liveness"])


# # ── Schemas ────────────────────────────────────────────────────────────────────

# class ChallengeResponse(BaseModel):
#     sequence: list[ChallengeStep]
#     nonce:    str


# class FrameEntropy(BaseModel):
#     """Passive liveness signals collected by the client."""
#     brightnessVariance:    float = Field(ge=0)
#     noiseVariance:         float = Field(ge=0)
#     headStabilityVariance: float = Field(ge=0)
#     earMicroVariance:      float = Field(ge=0)
#     blinkLatencyMs:        float = Field(ge=0)
#     turnLatencyMs:         float = Field(ge=0)


# class LivenessSubmission(BaseModel):
#     session_id:          str
#     nonce:               str
#     frame_base64:        str            # data URI  "data:image/jpeg;base64,..."
#     completed_sequence:  list[ChallengeStep]
#     frame_timestamps_ms: list[int]      # epoch-ms of each processed frame
#     entropy:             FrameEntropy
#     # Legacy booleans kept for backward-compat logging only — not trusted
#     blink_detected:      bool = False
#     head_turn_detected:  bool = False

#     @field_validator("frame_base64")
#     @classmethod
#     def validate_frame(cls, v: str) -> str:
#         if not v.startswith("data:image/"):
#             raise ValueError("frame_base64 must be a data URI")
#         # rough size check: 400 KB base64 ≈ 300 KB image
#         estimated_kb = len(v) * 3 / 4 / 1024
#         if estimated_kb > 500:
#             raise ValueError(f"Frame too large ({estimated_kb:.0f} KB) — max 500 KB")
#         return v

#     @field_validator("frame_timestamps_ms")
#     @classmethod
#     def validate_timestamps(cls, v: list[int]) -> list[int]:
#         if len(v) < 6:
#             raise ValueError("At least 6 frame timestamps required")
#         if len(v) > 500:
#             raise ValueError("Too many frame timestamps")
#         return v

#     @field_validator("completed_sequence")
#     @classmethod
#     def validate_sequence(cls, v: list[ChallengeStep]) -> list[ChallengeStep]:
#         if len(v) != 3:
#             raise ValueError("completed_sequence must contain exactly 3 steps")
#         return v


# class LivenessResult(BaseModel):
#     success:          bool
#     liveness_passed:  bool
#     message:          str
#     flagged:          bool = False
#     flag_reason:      str  = ""
#     elapsed_ms:       float = 0.0


# # ── Endpoints ──────────────────────────────────────────────────────────────────

# @router.get("/challenge", response_model=ChallengeResponse)
# async def get_challenge(
#     request: Request,
#     session=Depends(get_current_vendor_session),
# ) -> ChallengeResponse:
#     """
#     Issue a randomised liveness challenge for this session.
#     The client MUST complete steps in the returned order.
#     Idempotent: re-fetching within half the TTL returns the same challenge.
#     """
#     session_id = session.id
#     result     = await generate_challenge(session_id)
#     logger.info(f"Challenge issued | session={session_id} | ip={request.client.host}")
#     return ChallengeResponse(**result)


# @router.post("", response_model=LivenessResult)
# async def submit_liveness(
#     payload: LivenessSubmission,
#     request: Request,
#     session=Depends(get_current_vendor_session),
# ) -> LivenessResult:
#     """
#     Full liveness + identity verification pipeline:

#     1. Verify server-issued challenge (nonce, sequence, timing, entropy)
#     2. Forward selfie to Youverify NIN endpoint
#     3. Return composite identity result
#     """
#     session_id = session.id

#     # ── Step 1: challenge verification ───────────────────────────────────────
#     verify_result = await verify_challenge(
#         session_id=session_id,
#         nonce=payload.nonce,
#         completed_sequence=list(payload.completed_sequence),
#         frame_timestamps_ms=payload.frame_timestamps_ms,
#         entropy=payload.entropy.model_dump(),
#     )

#     if not verify_result.ok:
#         logger.warning(
#             f"Liveness challenge failed | session={session_id} "
#             f"| ip={request.client.host} | reason={verify_result.error}"
#         )
#         raise HTTPException(
#             status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
#             detail=verify_result.error,
#         )

#     # ── Step 2: Store selfie for identity verification ───────────────────────
#     await store_selfie(session_id, payload.frame_base64)

#     logger.info(
#         f"Liveness complete | session={session_id} "
#         f"| passed=True | flagged={verify_result.flagged} "
#         f"| elapsed={verify_result.elapsed_ms}ms"
#     )

#     return LivenessResult(
#         success=True,
#         liveness_passed=True,
#         message="Liveness verified and selfie captured.",
#         flagged=verify_result.flagged,
#         flag_reason=verify_result.flag_reason,
#         elapsed_ms=verify_result.elapsed_ms,
#     )


"""
routes/liveness.py
──────────────────
Liveness verification endpoints.

Flow:
  GET  /vendor/liveness/challenge   → issue nonce + random sequence
  POST /vendor/liveness             → verify challenge then call Youverify

Error contract:
  Every failure returns a JSON body:
    { "error_code": "SEQUENCE_MISMATCH", "detail": "human message", "scores": {...} }
  HTTP status codes:
    401 → NONCE_MISMATCH
    404 → SESSION_NOT_FOUND
    408 → SESSION_EXPIRED
    422 → SEQUENCE_MISMATCH | FRAME_TIMING_ANOMALY | COMPLETED_TOO_FAST
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings
from app.core.deps import get_current_vendor_session
from app.services.liveness_challenge import (
    ChallengeStep,
    LivenessErrorCode,
    VerificationScores,
    generate_challenge,
    verify_challenge,
    store_selfie,
)

logger   = logging.getLogger(__name__)
settings = get_settings()
router   = APIRouter(prefix="/vendor/liveness", tags=["liveness"])


# ── Error code → HTTP status mapping ──────────────────────────────────────────

_ERROR_STATUS: dict[str, int] = {
    LivenessErrorCode.SESSION_NOT_FOUND:    status.HTTP_404_NOT_FOUND,
    LivenessErrorCode.NONCE_MISMATCH:       status.HTTP_401_UNAUTHORIZED,
    LivenessErrorCode.SESSION_EXPIRED:      status.HTTP_408_REQUEST_TIMEOUT,
    LivenessErrorCode.COMPLETED_TOO_FAST:   status.HTTP_422_UNPROCESSABLE_ENTITY,
    LivenessErrorCode.SEQUENCE_MISMATCH:    status.HTTP_422_UNPROCESSABLE_ENTITY,
    LivenessErrorCode.FRAME_TIMING_ANOMALY: status.HTTP_422_UNPROCESSABLE_ENTITY,
    LivenessErrorCode.LOW_ENTROPY:          status.HTTP_422_UNPROCESSABLE_ENTITY,
}


# ── Schemas ────────────────────────────────────────────────────────────────────

class ChallengeResponse(BaseModel):
    sequence: list[ChallengeStep]
    nonce:    str


class FrameEntropy(BaseModel):
    brightnessVariance:    float = Field(ge=0)
    noiseVariance:         float = Field(ge=0)
    headStabilityVariance: float = Field(ge=0)
    earMicroVariance:      float = Field(ge=0)
    blinkLatencyMs:        float = Field(ge=0)
    turnLatencyMs:         float = Field(ge=0)


class LivenessSubmission(BaseModel):
    session_id:          str
    nonce:               str
    frame_base64:        str
    completed_sequence:  list[ChallengeStep]
    frame_timestamps_ms: list[int]
    entropy:             FrameEntropy
    blink_detected:      bool = False   # legacy, not trusted
    head_turn_detected:  bool = False   # legacy, not trusted

    @field_validator("frame_base64")
    @classmethod
    def validate_frame(cls, v: str) -> str:
        if not v.startswith("data:image/"):
            raise ValueError("frame_base64 must be a data URI")
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


class VerificationScoresSchema(BaseModel):
    """Scores in [0.0, 1.0] per verification dimension, plus composite."""
    sequence_match:  float
    timing_score:    float
    frame_quality:   float
    brightness:      float
    noise:           float
    head_stability:  float
    ear_movement:    float
    composite:       float


class LivenessResult(BaseModel):
    success:         bool
    liveness_passed: bool
    message:         str
    flagged:         bool                   = False
    flag_reason:     str                    = ""
    elapsed_ms:      float                  = 0.0
    scores:          VerificationScoresSchema | None = None


class LivenessErrorResponse(BaseModel):
    """Returned on any liveness verification failure."""
    error_code:  str                        # LivenessErrorCode.*
    detail:      str                        # Human-readable, safe to show in UI
    scores:      VerificationScoresSchema | None = None   # partial scores where available


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/challenge", response_model=ChallengeResponse)
async def get_challenge(
    request: Request,
    session=Depends(get_current_vendor_session),
) -> ChallengeResponse:
    session_id = session.id
    result     = await generate_challenge(session_id)
    logger.info(f"Challenge issued | session={session_id} | ip={request.client.host}")
    return ChallengeResponse(**result)


@router.post(
    "",
    response_model=LivenessResult,
    responses={
        401: {"model": LivenessErrorResponse, "description": "Invalid session nonce"},
        404: {"model": LivenessErrorResponse, "description": "Session not found or expired"},
        408: {"model": LivenessErrorResponse, "description": "Challenge timed out"},
        422: {"model": LivenessErrorResponse, "description": "Liveness check failed"},
    },
)
async def submit_liveness(
    payload: LivenessSubmission,
    request: Request,
    session=Depends(get_current_vendor_session),
) -> LivenessResult:
    """
    Full liveness + identity verification pipeline:

    1. Verify server-issued challenge (nonce, sequence, timing, entropy)
    2. Store selfie for downstream identity verification
    3. Return composite result with per-dimension scores

    On failure the response body always contains:
      { error_code, detail, scores }
    where `scores` carries per-dimension scores so the client can
    surface actionable feedback (e.g. "head too still", "wrong step order").
    """
    session_id = session.id

    verify_result = await verify_challenge(
        session_id=session_id,
        nonce=payload.nonce,
        completed_sequence=list(payload.completed_sequence),
        frame_timestamps_ms=payload.frame_timestamps_ms,
        entropy=payload.entropy.model_dump(),
    )

    if not verify_result.ok:
        http_status = _ERROR_STATUS.get(
            verify_result.error_code,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
        logger.warning(
            f"Liveness failed | session={session_id} | ip={request.client.host} "
            f"| error_code={verify_result.error_code} | detail={verify_result.error} "
            f"| composite_score={verify_result.scores.composite}"
        )
        return JSONResponse(
            status_code=http_status,
            content=LivenessErrorResponse(
                error_code=verify_result.error_code,
                detail=verify_result.error,
                scores=VerificationScoresSchema(**verify_result.scores.as_dict()),
            ).model_dump(),
        )

    await store_selfie(session_id, payload.frame_base64)

    logger.info(
        f"Liveness complete | session={session_id} "
        f"| composite_score={verify_result.scores.composite} "
        f"| flagged={verify_result.flagged} | elapsed={verify_result.elapsed_ms}ms"
    )

    return LivenessResult(
        success=True,
        liveness_passed=True,
        message="Liveness verified — selfie captured for identity check.",
        flagged=verify_result.flagged,
        flag_reason=verify_result.flag_reason,
        elapsed_ms=verify_result.elapsed_ms,
        scores=VerificationScoresSchema(**verify_result.scores.as_dict()),
    )