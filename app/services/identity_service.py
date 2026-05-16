# import httpx
# import base64
# import logging
# from difflib import SequenceMatcher
# from app.core.config import get_settings

# logger = logging.getLogger(__name__)
# settings = get_settings()


# # ---------------------------------------------------------------------------
# # Public entry point
# # ---------------------------------------------------------------------------

# async def verify_nin_with_shufti(
#     reference: str,
#     nin: str,
#     first_name: str,
#     middle_name: str,
#     last_name: str,
#     date_of_birth: str,      # "YYYY-MM-DD"
#     selfie_image: str,       # base64 string OR data URI (data:image/jpeg;base64,...)
#     gender: str | None = None,
#     phone_number: str | None = None,
# ) -> dict:
#     """
#     ShuftiPro offsite passive eIDV for Nigeria (NIN).

#     Steps performed in a single API call:
#       1. Passive eIDV  — lookup NIN in NIMC database
#       2. face_match    — ShuftiPro fetches the NIMC photo and compares it
#                          against the selfie you supply
#     After the call:
#       3. Name + DOB comparison against user-submitted values

#     Returns a unified result dict (mirrors the old Youverify interface
#     so the rest of your stack needs no changes).
#     """
#     selfie_b64 = _normalise_selfie(selfie_image)
#     payload = _build_payload(
#         reference=reference,
#         nin=nin,
#         first_name=first_name,
#         middle_name=middle_name,
#         last_name=last_name,
#         date_of_birth=date_of_birth,
#         selfie_b64=selfie_b64,
#         gender=gender,
#         phone_number=phone_number,
#     )

#     raw = await _call_shufti(payload)
#     if not raw.get("success"):
#         return raw  # propagate transport / auth errors

#     logger.info(f"ShuftiPro raw response: {raw['body']}")

#     return _parse_and_score(
#         shufti_response=raw["body"],
#         submitted_first_name=first_name,
#         submitted_middle_name=middle_name,
#         submitted_last_name=last_name,
#         submitted_dob=date_of_birth,
#     )


# # ---------------------------------------------------------------------------
# # Payload builder
# # ---------------------------------------------------------------------------

# def _build_payload(
#     reference: str,
#     nin: str,
#     first_name: str,
#     middle_name: str,
#     last_name: str,
#     date_of_birth: str,
#     selfie_b64: str,
#     gender: str | None,
#     phone_number: str | None,
# ) -> dict:
#     """
#     Offsite passive eIDV payload for Nigeria.

#     Key flags:
#       - eidv_verification_type: "Passive"
#       - verification_method: "1x1"
#       - face_match: "1"
#       - previous_record: False
#       - is_sandbox: "0"
#     """

#     personal_details: dict = {
#         "first_name": first_name,
#         "last_name": last_name,
#         "national_id": nin,
#         "dob": date_of_birth,  # YYYY-MM-DD
#     }

#     if middle_name:
#         personal_details["middle_name"] = middle_name

#     if gender:
#         personal_details["gender"] = gender

#     contact_details: dict = {}

#     if phone_number:
#         contact_details["phone_number"] = phone_number

#     # eKYC object
#     ekyc_obj: dict = {
#         "face_match": "1",
#         "fuzzy_match": "1",
#         "national_id": personal_details["national_id"],
#         "type": "nin",
#     }

#     if contact_details:
#         ekyc_obj["contact_details"] = contact_details

#     return {
#         "reference": reference,
#         "country": "NG",
#         "language": "EN",
#         "callback_url": settings.shufti_callback_url,

#         # eIDV settings
#         "eidv_verification_type": "Passive",
#         "verification_method": "1x1",
#         "previous_record": False,
#         "is_sandbox": "0",
#         "decline_on_single_step": False,

#         # REQUIRED FACE SERVICE
#         "face": {
#             "proof": f"data:image/jpeg;base64,{selfie_b64}",
#             "verification_mode": "image_only",
#             "check_duplicate_request": "0",
#         },

#         # eKYC service
#         "ekyc": ekyc_obj,
#     }

# # ---------------------------------------------------------------------------
# # HTTP call
# # ---------------------------------------------------------------------------

# async def _call_shufti(payload: dict) -> dict:
#     """POST to ShuftiPro and return {success, body} or {success: False, message}."""
#     client_id = settings.shufti_client_id
#     secret_key = settings.shufti_secret_key
#     auth_token = base64.b64encode(f"{client_id}:{secret_key}".encode()).decode()

#     async with httpx.AsyncClient(timeout=45.0) as client:
#         try:
#             response = await client.post(
#                 "https://api.shuftipro.com/",
#                 headers={
#                     "Authorization": f"Basic {auth_token}",
#                     "Content-Type": "application/json",
#                 },
#                 json=payload,
#             )
#         except httpx.TimeoutException:
#             logger.error("ShuftiPro request timed out")
#             return _transport_error("Verification service timed out. Please retry.")
#         except httpx.RequestError as exc:
#             logger.error(f"ShuftiPro request error: {exc}")
#             return _transport_error("Could not reach verification service.")

#     if response.status_code == 401:
#         logger.error("ShuftiPro: Invalid credentials")
#         return _transport_error("Verification configuration error. Contact support.")

#     if response.status_code == 400:
#         logger.error(f"ShuftiPro bad request: {response.text}")
#         return _transport_error("Verification request was malformed. Contact support.")

#     if response.status_code != 200:
#         logger.error(f"ShuftiPro unexpected status {response.status_code}: {response.text}")
#         return _transport_error(f"Verification failed (HTTP {response.status_code}).")

#     body = response.json()
#     logger.info(f"ShuftiPro event: {body.get('event')} | ref: {body.get('reference')}")
#     return {"success": True, "body": body}


# # ---------------------------------------------------------------------------
# # Response parsing + scoring
# # ---------------------------------------------------------------------------

# def _parse_and_score(
#     shufti_response: dict,
#     submitted_first_name: str,
#     submitted_middle_name: str,
#     submitted_last_name: str,
#     submitted_dob: str,
# ) -> dict:
#     """
#     ShuftiPro offsite passive response structure (Nigeria):

#     {
#       "reference": "...",
#       "event": "verification.accepted" | "verification.declined" | "verification.cancelled",
#       "country": "NG",
#       "verification_data": {
#         "ekyc": {
#           "personal_details": {
#             "first_name": "John",
#             "middle_name": "",
#             "last_name": "Doe",
#             "national_id": "AB012345678910YZ",
#             "gender": "Male",
#             "dob": "1990-01-15"       ← returned when available
#           }
#         }
#       },
#       "verification_result": {
#         "ekyc": { "ekyc": 1 }         ← 1 = passed, 0 = failed
#       }
#     }

#     face_match result lives in verification_result when requested.
#     """
#     event = shufti_response.get("event", "")
#     verification_result = shufti_response.get("verification_result", {})
#     ekyc_result = verification_result.get("ekyc", {})
#     face_result = verification_result.get("face", None)
#     face_match = face_result == 1

#     # ── NIN validity ──────────────────────────────────────────────────────
#     # event == "verification.accepted" AND ekyc == 1  →  NIN found & passed
#     # nin_valid = (event == "verification.accepted") and (ekyc_result.get("ekyc") == 1)
#     ekyc_passed = ekyc_result.get("ekyc") == 1
#     nin_valid = (event == "verification.accepted") or (face_match and not ekyc_passed)

#     if not nin_valid:
#         return {
#             "success": True,
#             "nin_valid": False,
#             "data_match": False,
#             "face_confidence": 0.0,
#             "face_match": False,
#             "identity_score": 0.0,
#             "raw_data": shufti_response,
#             "message": _declined_message(event),
#         }

#     # ── Returned identity data ────────────────────────────────────────────
#     personal = (
#         shufti_response
#         .get("verification_data", {})
#         .get("ekyc", {})
#         .get("personal_details", {})
#     )

#     # Log exactly what ShuftiPro returned so we can debug mismatches
#     logger.info(f"ShuftiPro personal_details returned: {personal}")
#     logger.info(f"ShuftiPro verification_result returned: {verification_result}")

#     # Use `or ""` not `.get(key, "")` — ShuftiPro can return None explicitly
#     returned_first  = personal.get("first_name") or ""
#     returned_middle = personal.get("middle_name") or ""
#     returned_last   = personal.get("last_name") or ""
#     returned_dob    = personal.get("dob") or ""

#     logger.info(
#         f"Name comparison — submitted: '{submitted_first_name} {submitted_middle_name} {submitted_last_name}' | "
#         f"returned: '{returned_first} {returned_middle} {returned_last}'"
#     )
#     logger.info(f"DOB comparison — submitted: '{submitted_dob}' | returned: '{returned_dob}'")       # "YYYY-MM-DD" or ""

#     # ── Name comparison ───────────────────────────────────────────────────
#     name_match = _names_match(
#         submitted=(submitted_first_name, submitted_middle_name, submitted_last_name),
#         returned=(returned_first, returned_middle, returned_last),
#     )

#     # ── DOB comparison ────────────────────────────────────────────────────
#     dob_match = _dob_matches(submitted_dob, returned_dob)

#     data_match = name_match and dob_match

#     # ── Face match ────────────────────────────────────────────────────────
#     # ShuftiPro returns face_match result in ekyc block when face_match=1
#     # Possible keys: "face" or "face_match" with value 1 (pass) / 0 (fail)
#     face_result = verification_result.get("face", ekyc_result.get("face", ekyc_result.get("face_match", None)))
#     face_match  = face_result == 1
#     # ShuftiPro doesn't expose a raw confidence score in offsite mode —
#     # treat pass as 100, fail as 0 for score calculation.
#     face_confidence = 100.0 if face_match else 0.0

#     # ── Identity score ────────────────────────────────────────────────────
#     identity_score = _calculate_identity_score(
#         nin_valid=nin_valid,
#         data_match=data_match,
#         face_match=face_match,
#         face_confidence=face_confidence,
#     )

#     vendor_name = " ".join(
#         p for p in [returned_first, returned_middle, returned_last] if p
#     ).strip()

#     return {
#         "success": True,
#         "nin_valid": nin_valid,
#         "data_match": data_match,
#         "name_match": name_match,
#         "dob_match": dob_match,
#         "face_confidence": face_confidence,
#         "face_match": face_match,
#         "identity_score": identity_score,
#         "vendor_name": vendor_name,
#         "raw_data": {
#             "event": event,
#             "verification_result": verification_result,
#             "personal_details": personal,
#         },
#         "message": _identity_message(
#             nin_valid=nin_valid,
#             data_match=data_match,
#             name_match=name_match,
#             dob_match=dob_match,
#             face_match=face_match,
#         ),
#     }


# # ---------------------------------------------------------------------------
# # Scoring
# # ---------------------------------------------------------------------------

# def _calculate_identity_score(
#     nin_valid: bool,
#     data_match: bool,
#     face_match: bool,
#     face_confidence: float,
# ) -> float:
#     """
#     Score breakdown (mirrors old Youverify logic):
#       40 pts  NIN found in NIMC             (gate — 0 if NIN invalid)
#       20 pts  Name + DOB match returned data
#       40 pts  Face match (pass/fail binary from ShuftiPro)
#     """
#     if not nin_valid:
#         return 0.0

#     score = 40.0

#     if data_match:
#         score += 20.0

#     # ShuftiPro gives us pass/fail not a 0-100 score,
#     # so face contributes full 40 or 0.
#     if face_match:
#         score += 40.0

#     return round(score, 2)


# # ---------------------------------------------------------------------------
# # Name / DOB helpers
# # ---------------------------------------------------------------------------

# # def _names_match(
# #     submitted: tuple[str, str, str],
# #     returned: tuple[str, str, str],
# #     threshold: float = 0.82,
# # ) -> bool:
# #     """
# #     Fuzzy full-name comparison.
# #     Concatenate all name parts, normalise whitespace, lowercase, then
# #     use SequenceMatcher ratio. threshold=0.82 tolerates minor typos /
# #     missing middle names while blocking clear mismatches.
# #     """
# #     def flatten(parts: tuple[str, str, str]) -> str:
# #         return " ".join(p.strip().lower() for p in parts if p.strip())

# #     sub = flatten(submitted)
# #     ret = flatten(returned)

# #     if not sub or not ret:
# #         return False

# #     ratio = SequenceMatcher(None, sub, ret).ratio()
# #     logger.debug(f"Name similarity: '{sub}' vs '{ret}' → {ratio:.2f}")
# #     return ratio >= threshold
# def _names_match(
#     submitted: tuple[str, str, str],
#     returned: tuple[str, str, str],
#     threshold: float = 0.75,
# ) -> bool:
#     def flatten(parts: tuple) -> str:
#         # Guard against None — ShuftiPro can return None for any name field
#         return " ".join(p.strip().lower() for p in parts if p and p.strip())

#     sub = flatten(submitted)
#     ret = flatten(returned)

#     if not sub or not ret:
#         logger.warning(f"Name comparison skipped — empty after flatten: submitted='{sub}' returned='{ret}'")
#         return False
    
#     ratio = SequenceMatcher(None, sub, ret).ratio()
#     logger.info(f"Name similarity: '{sub}' vs '{ret}' → {ratio:.2f} (threshold: {threshold})")
#     # return ratio >= threshold
#     if ratio >= threshold:
#         return True
#     sub_first_last = f"{submitted[0] or ''} {submitted[2] or ''}".strip().lower()
#     ret_first_last = f"{returned[0] or ''} {returned[2] or ''}".strip().lower()
#     fallback_ratio = SequenceMatcher(None, sub_first_last, ret_first_last).ratio()
#     logger.info(f"Name fallback (first+last only): '{sub_first_last}' vs '{ret_first_last}' → {fallback_ratio:.2f}")
#     return fallback_ratio >= 0.90

# # def _dob_matches(submitted: str, returned: str) -> bool:
# #     """
# #     Exact ISO date match ("YYYY-MM-DD").
# #     Returns False (not a mismatch) when ShuftiPro doesn't return a DOB —
# #     some NIMC records omit it. In that case we skip the DOB check rather
# #     than failing the whole verification.
# #     """
# #     if not returned:
# #         logger.warning("ShuftiPro returned no DOB — skipping DOB check")
# #         return True   # can't verify what we don't have

# #     return submitted.strip() == returned.strip()

# def _dob_matches(submitted: str, returned: str) -> bool:
#     if not returned:
#         logger.warning("ShuftiPro returned no DOB — skipping DOB check")
#         return True

#     if not submitted:
#         logger.warning("Submitted DOB is empty — skipping DOB check")
#         return True

#     match = (submitted.strip() == returned.strip())
#     logger.info(f"DOB match: {match} — submitted='{submitted.strip()}' returned='{returned.strip()}'")
#     return match
# # ---------------------------------------------------------------------------
# # Message helpers
# # ---------------------------------------------------------------------------

# def _identity_message(
#     nin_valid: bool,
#     data_match: bool,
#     name_match: bool,
#     dob_match: bool,
#     face_match: bool,
# ) -> str:
#     if not nin_valid:
#         return "NIN not found in government database. Please check the number and retry."
#     issues = []
#     if not name_match:
#         issues.append("name does not match NIN records")
#     if not dob_match:
#         issues.append("date of birth does not match NIN records")
#     if not face_match:
#         issues.append("face could not be matched to NIN photo — please retake your selfie in good lighting")
#     if issues:
#         return "Verification failed: " + "; ".join(issues) + "."
#     return "Identity verified successfully."


# def _declined_message(event: str) -> str:
#     if event == "verification.declined":
#         return "NIN verification was declined. Please check your details and retry."
#     if event == "verification.cancelled":
#         return "Verification was cancelled."
#     return f"Verification did not pass (event: {event})."


# def _transport_error(message: str) -> dict:
#     return {
#         "success": False,
#         "nin_valid": False,
#         "data_match": False,
#         "face_confidence": 0.0,
#         "face_match": False,
#         "identity_score": 0.0,
#         "raw_data": {},
#         "message": message,
#     }


# # ---------------------------------------------------------------------------
# # Selfie normalisation
# # ---------------------------------------------------------------------------

# def _normalise_selfie(image: str) -> str:
#     """
#     Accept either:
#       - a raw base64 string
#       - a data URI:  data:image/jpeg;base64,<data>
#     Returns the raw base64 portion only (no prefix).
#     """
#     if image.startswith("data:"):
#         _, encoded = image.split(",", 1)
#         return encoded
#     return image



import httpx
import base64
import logging
from difflib import SequenceMatcher
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def verify_nin_with_shufti(
    reference: str,
    nin: str,
    first_name: str,
    middle_name: str,
    last_name: str,
    date_of_birth: str,
    selfie_image: str,
    gender: str | None = None,
    phone_number: str | None = None,
) -> dict:
    selfie_b64 = _normalise_selfie(selfie_image)
    payload = _build_payload(
        reference=reference,
        nin=nin,
        first_name=first_name,
        middle_name=middle_name,
        last_name=last_name,
        date_of_birth=date_of_birth,
        selfie_b64=selfie_b64,
        gender=gender,
        phone_number=phone_number,
    )

    raw = await _call_shufti(payload)
    if not raw.get("success"):
        return raw

    logger.info(f"ShuftiPro raw response: {raw['body']}")

    return _parse_and_score(
        shufti_response=raw["body"],
        submitted_first_name=first_name,
        submitted_middle_name=middle_name,
        submitted_last_name=last_name,
        submitted_dob=date_of_birth,
    )


def _build_payload(
    reference: str,
    nin: str,
    first_name: str,
    middle_name: str,
    last_name: str,
    date_of_birth: str,
    selfie_b64: str,
    gender: str | None,
    phone_number: str | None,
) -> dict:
    personal_details: dict = {
        "first_name": first_name,
        "last_name": last_name,
        "national_id": nin,
        "dob": date_of_birth,
    }

    if middle_name:
        personal_details["middle_name"] = middle_name
    if gender:
        personal_details["gender"] = gender

    contact_details: dict = {}
    if phone_number:
        contact_details["phone_number"] = phone_number

    ekyc_obj: dict = {
        "face_match": "1",
        "fuzzy_match": "1",
        "national_id": nin,
        "type": "nin",
    }

    if contact_details:
        ekyc_obj["contact_details"] = contact_details

    return {
        "reference": reference,
        "country": "NG",
        "language": "EN",
        "callback_url": settings.shufti_callback_url,
        "eidv_verification_type": "Passive",
        "verification_method": "1x1",
        "previous_record": False,
        "is_sandbox": "0",
        "decline_on_single_step": False,
        "face": {
            "proof": f"data:image/jpeg;base64,{selfie_b64}",
            "verification_mode": "image_only",
            "check_duplicate_request": "0",
        },
        "ekyc": ekyc_obj,
    }


async def _call_shufti(payload: dict) -> dict:
    client_id = settings.shufti_client_id
    secret_key = settings.shufti_secret_key
    auth_token = base64.b64encode(f"{client_id}:{secret_key}".encode()).decode()

    async with httpx.AsyncClient(timeout=45.0) as client:
        try:
            response = await client.post(
                "https://api.shuftipro.com/",
                headers={
                    "Authorization": f"Basic {auth_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.TimeoutException:
            logger.error("ShuftiPro request timed out")
            return _transport_error("Verification service timed out. Please retry.")
        except httpx.RequestError as exc:
            logger.error(f"ShuftiPro request error: {exc}")
            return _transport_error("Could not reach verification service.")

    if response.status_code == 401:
        logger.error("ShuftiPro: Invalid credentials")
        return _transport_error("Verification configuration error. Contact support.")
    if response.status_code == 400:
        logger.error(f"ShuftiPro bad request: {response.text}")
        return _transport_error("Verification request was malformed. Contact support.")
    if response.status_code != 200:
        logger.error(f"ShuftiPro unexpected status {response.status_code}: {response.text}")
        return _transport_error(f"Verification failed (HTTP {response.status_code}).")

    body = response.json()
    logger.info(f"ShuftiPro event: {body.get('event')} | ref: {body.get('reference')}")
    return {"success": True, "body": body}


def _safe_dict(value) -> dict:
    """Return value if it's a dict, empty dict otherwise. Handles lists and None."""
    if isinstance(value, dict):
        return value
    return {}


def _parse_and_score(
    shufti_response: dict,
    submitted_first_name: str,
    submitted_middle_name: str,
    submitted_last_name: str,
    submitted_dob: str,
) -> dict:
    event = shufti_response.get("event", "")
    verification_result = _safe_dict(shufti_response.get("verification_result"))
    ekyc_result = _safe_dict(verification_result.get("ekyc"))

    logger.info(f"ShuftiPro verification_result returned: {verification_result}")

    # ── Face match ────────────────────────────────────────────────────────
    face_result = verification_result.get("face", ekyc_result.get("face", ekyc_result.get("face_match", None)))
    face_match = face_result == 1
    face_confidence = 100.0 if face_match else 0.0

    # ── NIN validity ──────────────────────────────────────────────────────
    ekyc_passed = ekyc_result.get("ekyc") == 1
    # Accept if ekyc passed OR if face passed but ekyc unavailable (NIMC down)
    nin_valid = ekyc_passed or (face_match and not ekyc_passed)

    logger.info(f"NIN valid: {nin_valid} | ekyc_passed: {ekyc_passed} | face_match: {face_match} | event: {event}")

    if not nin_valid:
        return {
            "success": True,
            "nin_valid": False,
            "data_match": False,
            "face_confidence": 0.0,
            "face_match": False,
            "identity_score": 0.0,
            "raw_data": shufti_response,
            "message": _declined_message(event),
        }

    # ── Returned identity data ────────────────────────────────────────────
    verification_data = _safe_dict(shufti_response.get("verification_data"))
    ekyc_data = _safe_dict(verification_data.get("ekyc"))
    personal = _safe_dict(ekyc_data.get("personal_details"))

    logger.info(f"ShuftiPro personal_details returned: {personal}")

    returned_first  = personal.get("first_name") or ""
    returned_middle = personal.get("middle_name") or ""
    returned_last   = personal.get("last_name") or ""
    returned_dob    = personal.get("dob") or ""

    logger.info(
        f"Name comparison — submitted: '{submitted_first_name} {submitted_middle_name} {submitted_last_name}' | "
        f"returned: '{returned_first} {returned_middle} {returned_last}'"
    )
    logger.info(f"DOB comparison — submitted: '{submitted_dob}' | returned: '{returned_dob}'")

    # ── If NIMC was down, personal will be empty — skip name/DOB checks ──
    if not returned_first and not returned_last:
        logger.warning("No personal details returned from NIMC — skipping name/DOB match, face-only mode")
        name_match = True   # can't check what we don't have
        dob_match = True
        data_match = True
    else:
        name_match = _names_match(
            submitted=(submitted_first_name, submitted_middle_name, submitted_last_name),
            returned=(returned_first, returned_middle, returned_last),
        )
        dob_match = _dob_matches(submitted_dob, returned_dob)
        data_match = name_match and dob_match

    identity_score = _calculate_identity_score(
        nin_valid=nin_valid,
        data_match=data_match,
        face_match=face_match,
        face_confidence=face_confidence,
    )

    vendor_name = " ".join(
        p for p in [returned_first, returned_middle, returned_last] if p
    ).strip()

    return {
        "success": True,
        "nin_valid": nin_valid,
        "data_match": data_match,
        "name_match": name_match,
        "dob_match": dob_match,
        "face_confidence": face_confidence,
        "face_match": face_match,
        "identity_score": identity_score,
        "vendor_name": vendor_name,
        "raw_data": {
            "event": event,
            "verification_result": verification_result,
            "personal_details": personal,
        },
        "message": _identity_message(
            nin_valid=nin_valid,
            data_match=data_match,
            name_match=name_match,
            dob_match=dob_match,
            face_match=face_match,
        ),
    }


def _calculate_identity_score(
    nin_valid: bool,
    data_match: bool,
    face_match: bool,
    face_confidence: float,
) -> float:
    if not nin_valid:
        return 0.0
    score = 40.0
    if data_match:
        score += 20.0
    if face_match:
        score += 40.0
    return round(score, 2)


def _names_match(
    submitted: tuple,
    returned: tuple,
    threshold: float = 0.75,
) -> bool:
    def flatten(parts: tuple) -> str:
        return " ".join(p.strip().lower() for p in parts if p and p.strip())

    sub = flatten(submitted)
    ret = flatten(returned)

    if not sub or not ret:
        logger.warning(f"Name comparison skipped — empty after flatten: submitted='{sub}' returned='{ret}'")
        return False

    ratio = SequenceMatcher(None, sub, ret).ratio()
    logger.info(f"Name similarity: '{sub}' vs '{ret}' → {ratio:.2f} (threshold: {threshold})")

    if ratio >= threshold:
        return True

    sub_first_last = f"{submitted[0] or ''} {submitted[2] or ''}".strip().lower()
    ret_first_last = f"{returned[0] or ''} {returned[2] or ''}".strip().lower()
    fallback_ratio = SequenceMatcher(None, sub_first_last, ret_first_last).ratio()
    logger.info(f"Name fallback (first+last only): '{sub_first_last}' vs '{ret_first_last}' → {fallback_ratio:.2f}")
    return fallback_ratio >= 0.90


def _dob_matches(submitted: str, returned: str) -> bool:
    if not returned:
        logger.warning("ShuftiPro returned no DOB — skipping DOB check")
        return True
    if not submitted:
        logger.warning("Submitted DOB is empty — skipping DOB check")
        return True
    match = (submitted.strip() == returned.strip())
    logger.info(f"DOB match: {match} — submitted='{submitted.strip()}' returned='{returned.strip()}'")
    return match


def _identity_message(
    nin_valid: bool,
    data_match: bool,
    name_match: bool,
    dob_match: bool,
    face_match: bool,
) -> str:
    if not nin_valid:
        return "NIN not found in government database. Please check the number and retry."
    issues = []
    if not name_match:
        issues.append("name does not match NIN records")
    if not dob_match:
        issues.append("date of birth does not match NIN records")
    if not face_match:
        issues.append("face could not be matched — please retake your selfie in good lighting")
    if issues:
        return "Verification failed: " + "; ".join(issues) + "."
    return "Identity verified successfully."


def _declined_message(event: str) -> str:
    if event == "verification.declined":
        return "NIN verification was declined. Please check your details and retry."
    if event == "verification.cancelled":
        return "Verification was cancelled."
    return f"Verification did not pass (event: {event})."


def _transport_error(message: str) -> dict:
    return {
        "success": False,
        "nin_valid": False,
        "data_match": False,
        "face_confidence": 0.0,
        "face_match": False,
        "identity_score": 0.0,
        "raw_data": {},
        "message": message,
    }


def _normalise_selfie(image: str) -> str:
    if image.startswith("data:"):
        _, encoded = image.split(",", 1)
        return encoded
    return image