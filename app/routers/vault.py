from fastapi import APIRouter, HTTPException
from app.models.schemas import (
    LivenessCheckRequest, LivenessCheckResponse,
    VoiceChallengeStartRequest, VoiceChallengeStartResponse,
    VoiceChallengeVerifyRequest, VoiceChallengeVerifyResponse,
    IdentityVerifyRequest, IdentityVerifyResponse,
    VaultScoreResponse,
    DeliveryConfirmRequest, DeliveryConfirmResponse, VaultScoreRequest
)
from app.services.voice_service import (
    get_challenge_phrase,
    get_assemblyai_realtime_token,
    verify_voice_challenge,
)
from app.services.identity_service import verify_nin_with_shufti
from app.services.vault_score_service import calculate_vault_score
from app.services.escrow_service import release_escrow
from app.services.liveness_challenge import get_selfie
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


# ════════════════════════════════════════════════════════════════
# PIPELINE 1B — VOICE CHALLENGE
# ════════════════════════════════════════════════════════════════

@router.post(
    "/vendor/voice/start",
    response_model=VoiceChallengeStartResponse,
    summary="Step 2a: Get challenge phrase + AssemblyAI token",
    description="""
    Call this after liveness passes.

    Returns:
    - challenge_phrase: the phrase vendor must read aloud
    - assemblyai_token: temporary token (60s) for frontend WebSocket
    - websocket_url: AssemblyAI real-time WebSocket URL

    Frontend responsibility:
    - Connect to websocket_url with the token
    - Stream microphone audio to AssemblyAI
    - Receive real-time transcript
    - Call /vendor/voice/verify with final transcript
    """
)
async def start_voice_challenge(payload: VoiceChallengeStartRequest):
    try:
        token_data = await get_assemblyai_realtime_token()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Voice service unavailable: {str(e)}")

    phrase = get_challenge_phrase(payload.session_id)

    return VoiceChallengeStartResponse(
        success=True,
        session_id=payload.session_id,
        challenge_phrase=phrase,
        assemblyai_token=token_data["token"],
        websocket_url=token_data["websocket_url"]
    )


@router.post(
    "/vendor/voice/verify",
    response_model=VoiceChallengeVerifyResponse,
    summary="Step 2b: Verify transcript from AssemblyAI",
    description="""
    Call this after AssemblyAI returns the final transcript.

    Frontend responsibility:
    - Close WebSocket after phrase is spoken
    - Send final transcript + confidence + multiple_speakers_detected

    Backend checks:
    - 60%+ key words from challenge phrase present in transcript
    - Audio confidence >= 0.35
    - No coaching voice detected (multiple_speakers = false)
    """
)
async def verify_voice(payload: VoiceChallengeVerifyRequest):
    result = await verify_voice_challenge(
        session_id=payload.session_id,
        transcript=payload.transcript,
        audio_confidence=payload.audio_confidence,
        multiple_speakers=payload.multiple_speakers_detected
    )
    return VoiceChallengeVerifyResponse(
        success=True,
        session_id=payload.session_id,
        voice_passed=result["voice_passed"],
        phrase_matched=result["phrase_matched"],
        coaching_detected=result["coaching_detected"],
        confidence_score=result["voice_score"],
        message=result["message"]
    )


# ════════════════════════════════════════════════════════════════
# PIPELINE 1C — IDENTITY VERIFICATION (Youverify)
# ════════════════════════════════════════════════════════════════

@router.post(
    "/vendor/verify-identity",
    response_model=IdentityVerifyResponse,
    summary="Step 3: NIN + face match via ShuftiPro",
    description="""
    Call this ONLY after liveness + voice have both passed.

    Sends to ShuftiPro in one passive eIDV API call:
    - NIN lookup against NIMC database
    - Name + DOB comparison against returned government record
    - Selfie face match against NIMC photo (via face_match flag)

    The selfie_image should be the same base64 frame
    captured during the liveness check in Step 1.

    Returns identity_score (0-100) which feeds VaultScore.
    """
)
async def verify_identity(payload: IdentityVerifyRequest):
    selfie_image = payload.selfie_image

    # If selfie not provided in payload, try to retrieve it from session storage
    if not selfie_image:
        selfie_image = await get_selfie(payload.session_id)

    if not selfie_image:
        logger.warning(f"Identity verification failed | session={payload.session_id} | reason=No selfie found")
        raise HTTPException(
            status_code=400,
            detail="No captured selfie found for this session. Please complete liveness check first."
        )

    result = await verify_nin_with_shufti(
        reference=payload.session_id,
        nin=payload.nin,
        first_name=payload.first_name,
        middle_name=payload.middle_name,
        last_name=payload.last_name,
        date_of_birth=payload.date_of_birth,
        selfie_image=selfie_image,
    )

    if not result["success"]:
        raise HTTPException(status_code=503, detail=result["message"])

    # Calculate preliminary VaultScore after identity
    # (no transaction history yet for new vendors)
    # identity_score = result["identity_score"]
    
    return IdentityVerifyResponse(
        success=True,
        session_id=payload.session_id,
        identity_passed=result["nin_valid"] and result["face_match"],
        nin_valid=result["nin_valid"],
        data_match=result["data_match"],
        face_confidence=result["face_confidence"],
        face_match=result["face_match"],
        identity_score=result["identity_score"],
        message=result["message"]
    )

# ════════════════════════════════════════════════════════════════
# VAULT SCORE
# ════════════════════════════════════════════════════════════════

@router.post(
    "/vendor/vault-score",
    response_model=VaultScoreResponse,
    summary="Calculate final VaultScore after all 3 verification steps",
    description="""
    Called internally by completeSession after identity + liveness + voice
    have all passed. Accepts real scores from the session rather than
    using hardcoded placeholders.
    """
)
async def get_vault_score(payload: VaultScoreRequest):
    score_result = calculate_vault_score(
        identity_score=payload.identity_score,
        liveness_confidence=payload.liveness_confidence,
        voice_score=payload.voice_score,
        total_orders=payload.total_orders or 0,
        successful_deliveries=payload.successful_deliveries or 0,
        total_disputes=payload.total_disputes or 0,
    )
    return VaultScoreResponse(
        success=True,
        vendor_id=payload.vendor_id,
        vault_score=score_result["vault_score"],
        score_breakdown=score_result["score_breakdown"],
        trust_level=score_result["trust_level"],
        verified=score_result["verified"]
    )


# ════════════════════════════════════════════════════════════════
# PIPELINE 2 — DELIVERY CONFIRMATION & GPS VERIFICATION
# ════════════════════════════════════════════════════════════════
# Escrow creation (Squad Virtual Account) is handled by the JS backend.
# Python backend verifies GPS and signals JS backend to release funds.
# ════════════════════════════════════════════════════════════════


@router.post(
    "/order/confirm-delivery",
    response_model=DeliveryConfirmResponse,
    summary="Step 4: Verify GPS coordinates for delivery",
    description="""
    Both vendor and buyer submit GPS coordinates.
    Python backend verifies distance between delivery locations.
    
    If within 500m of each other → gps_verified=true.
    JS backend checks this response and calls Squad API to release escrow.
    If GPS mismatch → gps_verified=false, order flagged for manual review.
    """
)
async def confirm_delivery(payload: DeliveryConfirmRequest):
    result = await release_escrow(
        order_id=payload.order_id,
        vendor_lat=payload.vendor_lat,
        vendor_lng=payload.vendor_lng,
        buyer_lat=payload.buyer_lat,
        buyer_lng=payload.buyer_lng
    )
    return DeliveryConfirmResponse(**result)
