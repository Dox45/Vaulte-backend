import httpx
import random
import re
import logging
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ─── Challenge Phrases ────────────────────────────────────────────────────────
# Random phrases vendor must read aloud.
# Rotated per session to prevent replay attacks.
# Must be natural to speak but hard to pre-record generically.

CHALLENGE_PHRASES = [
    "I am verifying my Vault account today",
    "My store is registered and verified on Vault",
    "I confirm this is my identity for Vault verification",
    "Vault security check is happening right now",
    "I am a real vendor setting up my Vault store",
    "My business is being verified on Vault platform",
    "I agree to the Vault vendor terms and conditions",
    "This verification is being done by me personally",
]


def get_challenge_phrase(session_id: str) -> str:
    """
    Deterministic phrase selection per session.
    Same session always gets same phrase (for retries),
    but different sessions get different phrases.
    """
    index = hash(session_id) % len(CHALLENGE_PHRASES)
    return CHALLENGE_PHRASES[index]


async def get_assemblyai_realtime_token() -> dict:
    """
    Get a temporary AssemblyAI token for real-time transcription.
    Frontend uses this token to open a WebSocket directly to AssemblyAI.
    Token expires after 60 seconds — enough for a voice challenge.

    AssemblyAI real-time API v3:
    GET https://streaming.assemblyai.com/v3/token?expires_in_seconds=60
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://streaming.assemblyai.com/v3/token",
            headers={
                "Authorization": settings.assemblyai_api_key
            },
            params={"expires_in_seconds": 480}
        )

        if response.status_code != 200:
            logger.error(f"AssemblyAI token error: {response.text}")
            raise Exception(f"Failed to get AssemblyAI token: {response.status_code}")

        data = response.json()
        token = data["token"]
        # Frontend connects to this WebSocket URL with the token
        # New v3 API uses speech_model parameter instead of sample_rate
        # websocket_url = f"wss://streaming.assemblyai.com/v3/ws?token={token}&speech_model=universal-streaming-english"
        websocket_url = f"wss://streaming.assemblyai.com/v3/ws?token={token}&speech_model=universal-streaming-english&sample_rate=16000"
        
        return {
            "token": token,
            "websocket_url": websocket_url
        }


def verify_phrase_match(transcript: str, expected_phrase: str) -> dict:
    """
    Fuzzy phrase matching — vendor doesn't need to say it perfectly.
    Check for key words rather than exact match to handle
    accents, slight mispronunciations, and background noise.

    Returns match result and a score.
    """
    if not transcript:
        return {
            "matched": False,
            "match_score": 0.0,
            "reason": "Empty transcript received"
        }

    # Normalize both strings
    transcript_clean = transcript.lower().strip()
    phrase_clean = expected_phrase.lower().strip()

    # Exact match
    if transcript_clean == phrase_clean:
        return {"matched": True, "match_score": 1.0, "reason": "Exact match"}

    # Extract key words from the phrase (words > 3 chars, skip filler words)
    filler_words = {"the", "and", "for", "this", "that", "is", "am", "are", "my", "on"}
    key_words = [
        word for word in re.findall(r'\b\w+\b', phrase_clean)
        if len(word) > 3 and word not in filler_words
    ]

    if not key_words:
        return {"matched": False, "match_score": 0.0, "reason": "No key words in phrase"}

    # Count how many key words appear in the transcript
    matched_words = [
        word for word in key_words
        if word in transcript_clean
    ]

    match_score = len(matched_words) / len(key_words)

    # Require at least 60% of key words to match
    matched = match_score >= 0.60

    return {
        "matched": matched,
        "match_score": round(match_score, 3),
        "matched_words": matched_words,
        "total_key_words": len(key_words),
        "reason": (
            f"Matched {len(matched_words)}/{len(key_words)} key words"
        )
    }


def detect_coaching_risk(
    multiple_speakers: bool,
    audio_confidence: float,
    transcript: str
) -> dict:
    """
    Detect signs of coached verification.

    Signals:
    - Multiple speakers in audio (someone whispering the phrase)
    - Very low audio confidence (muffled, distant audio)
    - Transcript contains unusual hesitations or repetitions
    """
    risk_signals = []
    risk_score = 0.0

    if multiple_speakers:
        risk_signals.append("Multiple speakers detected in audio")
        risk_score += 0.60

    if audio_confidence < 0.40:
        risk_signals.append("Unusually low audio confidence — possible distant or muffled speaker")
        risk_score += 0.25

    # Check for repeated word patterns — sign of reading haltingly from a screen
    # coached by someone else
    words = transcript.lower().split()
    if len(words) > 3:
        word_set = set(words)
        repetition_ratio = 1 - (len(word_set) / len(words))
        if repetition_ratio > 0.40:
            risk_signals.append("High word repetition detected in transcript")
            risk_score += 0.20

    coaching_detected = risk_score >= 0.50

    return {
        "coaching_detected": coaching_detected,
        "risk_score": round(min(risk_score, 1.0), 3),
        "risk_signals": risk_signals
    }


async def verify_voice_challenge(
    session_id: str,
    transcript: str,
    audio_confidence: float,
    multiple_speakers: bool
) -> dict:
    """
    Full voice verification pipeline:
    1. Match transcript against expected challenge phrase
    2. Check for coaching signals
    3. Return composite voice score
    """
    expected_phrase = get_challenge_phrase(session_id)

    phrase_result = verify_phrase_match(transcript, expected_phrase)
    coaching_result = detect_coaching_risk(
        multiple_speakers, audio_confidence, transcript
    )

    # Voice passes if:
    # - Phrase matched (60%+ key words)
    # - Audio confidence >= 0.10 (filters out true silence/garbage only)
    # - No coaching detected
    voice_passed = (
        phrase_result["matched"]
        and audio_confidence >= 0.10
        and not coaching_result["coaching_detected"]
    )

    # Voice score: weighted combination
    voice_score = (
        phrase_result["match_score"] * 0.60
        + audio_confidence * 0.25
        + (0.0 if coaching_result["coaching_detected"] else 0.15)
    )

    return {
        "voice_passed": voice_passed,
        "phrase_matched": phrase_result["matched"],
        "match_score": phrase_result["match_score"],
        "audio_confidence": audio_confidence,
        "coaching_detected": coaching_result["coaching_detected"],
        "coaching_risk_score": coaching_result["risk_score"],
        "voice_score": round(voice_score, 3),
        "expected_phrase": expected_phrase,
        "message": (
            "Voice verification passed"
            if voice_passed
            else _voice_failure_message(phrase_result, coaching_result, audio_confidence)
        )
    }


def _voice_failure_message(phrase_result, coaching_result, audio_confidence) -> str:
    if coaching_result["coaching_detected"]:
        return "Suspicious audio detected. Please verify alone in a quiet environment."
    if not phrase_result["matched"]:
        return f"Phrase not matched clearly. Please read the phrase aloud clearly."
    if audio_confidence < 0.10:
        return "Audio quality too low. Please move to a quieter location and retry."
    return "Voice verification failed. Please retry."
