from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum


# ─── Liveness ────────────────────────────────────────────────────────────────

class LivenessCheckRequest(BaseModel):
    """
    Frontend sends the captured video frame as base64
    after MediaPipe confirms blink + head turn passed client-side.
    The backend re-validates the frame using MediaPipe server-side
    as a second layer of trust.
    """
    session_id: str = Field(..., description="Unique verification session ID")
    frame_base64: str = Field(..., description="Base64 encoded image frame from MediaPipe capture")
    blink_detected: bool = Field(..., description="MediaPipe blink detection result from frontend")
    head_turn_detected: bool = Field(..., description="MediaPipe head turn detection result from frontend")


class LivenessCheckResponse(BaseModel):
    success: bool
    session_id: str
    liveness_passed: bool
    face_detected: bool
    confidence_score: float
    message: str


# ─── Voice ───────────────────────────────────────────────────────────────────

class VoiceChallengeStartRequest(BaseModel):
    session_id: str = Field(..., description="Must match liveness session ID")


class VoiceChallengeStartResponse(BaseModel):
    success: bool
    session_id: str
    challenge_phrase: str = Field(..., description="Phrase vendor must read aloud")
    assemblyai_token: str = Field(..., description="Temporary AssemblyAI token for real-time transcription")
    websocket_url: str = Field(..., description="AssemblyAI WebSocket URL for frontend to connect")


class VoiceChallengeVerifyRequest(BaseModel):
    session_id: str
    transcript: str = Field(..., description="Final transcript from AssemblyAI sent by frontend")
    audio_confidence: float = Field(..., description="AssemblyAI confidence score 0-1")
    multiple_speakers_detected: bool = Field(
        default=False,
        description="True if AssemblyAI detected more than one voice — coaching fraud signal"
    )


class VoiceChallengeVerifyResponse(BaseModel):
    success: bool
    session_id: str
    voice_passed: bool
    phrase_matched: bool
    coaching_detected: bool
    confidence_score: float
    message: str


# ─── Identity (Youverify NIN) ─────────────────────────────────────────────────

class IdentityVerifyRequest(BaseModel):
    """
    Frontend sends this ONLY after liveness + voice have passed.
    The selfie_image is the same frame captured during liveness.
    """
    session_id: str
    nin: str = Field(..., min_length=11, max_length=11, description="11-digit NIN")
    first_name: str
    last_name: str
    date_of_birth: str = Field(..., description="Format: YYYY-MM-DD")
    selfie_image: str = Field(
        ...,
        description="Base64 image or URL of face captured during liveness check"
    )


class IdentityVerifyResponse(BaseModel):
    success: bool
    session_id: str
    identity_passed: bool
    nin_valid: bool
    data_match: bool
    face_confidence: float
    face_match: bool
    identity_score: float = Field(..., description="0-100 score from identity signals")
    vault_score: Optional[float] = Field(None, description="Preliminary VaultScore after identity")
    message: str


# ─── VaultScore ──────────────────────────────────────────────────────────────

class VaultScoreResponse(BaseModel):
    success: bool
    vendor_id: str
    vault_score: float = Field(..., description="0-100 evolving trust score")
    score_breakdown: dict = Field(..., description="Individual signal weights")
    trust_level: str = Field(..., description="LOW | MEDIUM | HIGH | VERIFIED")
    verified: bool


# ─── Delivery Confirmation & GPS Verification ───────────────────────────────────

class DeliveryConfirmRequest(BaseModel):
    order_id: str
    vendor_lat: float = Field(..., description="Vendor GPS at delivery point")
    vendor_lng: float = Field(..., description="Vendor GPS at delivery point")
    buyer_lat: float = Field(..., description="Buyer GPS at receipt")
    buyer_lng: float = Field(..., description="Buyer GPS at receipt")
    delivery_photo_base64: Optional[str] = Field(
        None,
        description="Photo of goods at handoff for fraud detection"
    )


class DeliveryConfirmResponse(BaseModel):
    success: bool
    order_id: str
    gps_verified: bool
    distance_metres: float
    message: str = Field(..., description="If gps_verified=true, JS backend should release escrow to vendor via Squad API")
