import httpx
import logging
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def verify_nin_with_face(
    nin: str,
    first_name: str,
    last_name: str,
    date_of_birth: str,
    selfie_image: str
) -> dict:
    """
    Single Youverify API call that does three things:
    1. Verifies NIN exists in NIMC database
    2. Validates name + DOB against government record
    3. Compares selfie against NIMC photo

    POST /v2/api/identity/ng/nin

    selfie_image: base64 string or CDN URL of captured liveness frame
    """
    # Youverify requires a valid URI (data URI or http URL).
    # Since the frontend sends a data URI (data:image/jpeg;base64,...), we pass it directly.
    estimated_kb = len(selfie_image) * 3 / 4 / 1024
    logger.info(f"Selfie image size: ~{estimated_kb:.0f} KB")
    if estimated_kb > 400:
        logger.warning(f"Selfie may be too large ({estimated_kb:.0f} KB) — Youverify may reject it")

    payload = {
        "id": nin,
        "premiumNin": True,
        "isSubjectConsent": True,
        "validations": {
            "data": {
                "firstName": first_name,
                "lastName": last_name,
                "dateOfBirth": date_of_birth
            },
            "selfie": {
                "image": selfie_image
            }
        }
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                f"{settings.youverify_base_url}/v2/api/identity/ng/nin",
                headers={
                    "token": settings.youverify_token,
                    "Content-Type": "application/json"
                },
                json=payload
            )
        except httpx.TimeoutException:
            logger.error("Youverify request timed out")
            return _error_result("Verification service timed out. Please retry.")
        except httpx.RequestError as e:
            logger.error(f"Youverify request error: {e}")
            return _error_result("Could not reach verification service.")

    # Handle HTTP errors
    if response.status_code == 402:
        logger.error("Youverify: Insufficient funds in wallet")
        return _error_result("Verification service unavailable. Contact support.")

    if response.status_code == 403:
        logger.error("Youverify: Invalid API token")
        return _error_result("Verification configuration error. Contact support.")

    if response.status_code == 500:
        logger.error("Youverify: Internal server error")
        return _error_result("Verification service temporarily unavailable. Please retry.")

    if response.status_code != 200:
        logger.error(f"Youverify unexpected status: {response.status_code} — {response.text}")
        return _error_result(f"Verification failed with status {response.status_code}")

    data = response.json().get("data", {})
    return _parse_youverify_response(data)


def _parse_youverify_response(data: dict) -> dict:
    """
    Parse Youverify NIN response into clean Vault signals.

    Key fields:
    - status: "found" | "not_found"
    - dataValidation: bool — name + DOB matched government record
    - selfieValidation: bool — selfie was processed
    - validations.selfie.selfieVerification.confidenceLevel: 0-100
    - validations.selfie.selfieVerification.match: bool (threshold 80)
    - validations.selfie.selfieVerification.threshold: 80
    """
    nin_valid = data.get("status") == "found"

    if not nin_valid:
        return {
            "success": True,
            "nin_valid": False,
            "data_match": False,
            "face_confidence": 0.0,
            "face_match": False,
            "identity_score": 0.0,
            "raw_data": data,
            "message": "NIN not found in government database"
        }

    # Data validation — name + DOB match
    data_match = data.get("dataValidation", False)

    # Face match
    selfie_verification = (
        data
        .get("validations", {})
        .get("selfie", {})
        .get("selfieVerification", {})
    )

    face_confidence = selfie_verification.get("confidenceLevel", 0)
    face_match = selfie_verification.get("match", False)
    face_threshold = selfie_verification.get("threshold", 80)

    # Handle -1 confidence (face couldn't be processed)
    if face_confidence == -1:
        face_confidence = 0.0
        face_match = False

    # Calculate identity score (0-100)
    identity_score = _calculate_identity_score(
        nin_valid=nin_valid,
        data_match=data_match,
        face_confidence=face_confidence,
        face_match=face_match
    )

    return {
        "success": True,
        "nin_valid": nin_valid,
        "data_match": data_match,
        "face_confidence": float(face_confidence),
        "face_match": face_match,
        "face_threshold": face_threshold,
        "identity_score": identity_score,
        "vendor_name": f"{data.get('firstName', '')} {data.get('lastName', '')}".strip(),
        "raw_data": {
            "status": data.get("status"),
            "allValidationPassed": data.get("allValidationPassed"),
            "validationMessages": data.get("validations", {}).get("validationMessages", "")
        },
        "message": _identity_message(nin_valid, data_match, face_match, face_confidence)
    }


def _calculate_identity_score(
    nin_valid: bool,
    data_match: bool,
    face_confidence: float,
    face_match: bool
) -> float:
    """
    Identity Score formula:
    - NIN valid:      40 points (non-negotiable — base requirement)
    - Data match:     20 points (name + DOB match government record)
    - Face match:     40 points (scaled by confidence level / 100)

    Score of 0 if NIN is invalid — identity fails at the gate.
    """
    if not nin_valid:
        return 0.0

    score = 40.0  # NIN valid baseline

    if data_match:
        score += 20.0

    # Face score is proportional to confidence, not just pass/fail
    # Even a 70% confidence (below 80 threshold) contributes partial score
    face_score = (face_confidence / 100) * 40
    score += face_score

    return round(score, 2)


def _identity_message(nin_valid, data_match, face_match, face_confidence) -> str:
    if not nin_valid:
        return "NIN not found. Please check the number and retry."
    if not data_match and not face_match:
        return "Name, date of birth, and face do not match NIN records."
    if not data_match:
        return "Name or date of birth does not match NIN records."
    if not face_match:
        return f"Face match failed (confidence: {face_confidence}%). Please retake your selfie in good lighting."
    return "Identity verified successfully."


def _error_result(message: str) -> dict:
    return {
        "success": False,
        "nin_valid": False,
        "data_match": False,
        "face_confidence": 0.0,
        "face_match": False,
        "identity_score": 0.0,
        "raw_data": {},
        "message": message
    }
