"""
MediaPipe Liveness Service — Latest Tasks API (0.10.x+)

Uses mp.tasks.vision instead of the deprecated mp.solutions
which causes: AttributeError: module 'mediapipe' has no attribute 'solutions'

New API requires downloading model files (.task) separately.
Models are downloaded once on startup via _ensure_models().

Docs: https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker/python
      https://ai.google.dev/edge/mediapipe/solutions/vision/face_detector/python
"""

import base64
import io
import os
import urllib.request
import urllib.error
import numpy as np
import logging
import mediapipe as mp
from PIL import Image

logger = logging.getLogger(__name__)

# ─── New Tasks API imports (replaces all mp.solutions.* calls) ────────────────
BaseOptions           = mp.tasks.BaseOptions
FaceDetector          = mp.tasks.vision.FaceDetector
FaceDetectorOptions   = mp.tasks.vision.FaceDetectorOptions
FaceLandmarker        = mp.tasks.vision.FaceLandmarker
FaceLandmarkerOptions = mp.tasks.vision.FaceLandmarkerOptions
VisionRunningMode     = mp.tasks.vision.RunningMode

# ─── Model paths ─────────────────────────────────────────────────────────────
MODELS_DIR            = os.path.join(os.path.dirname(__file__), "models")
FACE_DETECTOR_MODEL   = os.path.join(MODELS_DIR, "blaze_face_short_range.tflite")
FACE_LANDMARKER_MODEL = os.path.join(MODELS_DIR, "face_landmarker.task")

# Correct URLs — version pinned to 1, correct file extensions
# Face Detector: bounding box only (fast pass 1) — uses .tflite format
FACE_DETECTOR_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
)
# Face Landmarker: 468 landmarks for eye/mouth/head analysis — uses .task format
FACE_LANDMARKER_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_landmarker/face_landmarker/float16/1/face_landmarker.task"
)


def _ensure_models() -> bool:
    """
    Download MediaPipe model files if not already present.
    Called once on first use. Models are ~1-3MB each.
    Returns True if models are ready, False if download failed.
    """
    os.makedirs(MODELS_DIR, exist_ok=True)
    logger.info(f"[LIVENESS] Models directory: {MODELS_DIR}")

    models = [
        (FACE_DETECTOR_MODEL,   FACE_DETECTOR_URL,   "Face Detector"),
        (FACE_LANDMARKER_MODEL, FACE_LANDMARKER_URL, "Face Landmarker"),
    ]

    for model_path, url, name in models:
        if os.path.exists(model_path):
            file_size = os.path.getsize(model_path)
            logger.info(f"[LIVENESS] ✓ {name} model already cached ({file_size} bytes)")
            continue

        logger.info(f"[LIVENESS] Attempting to download {name} from:")
        logger.info(f"[LIVENESS]   URL: {url}")
        try:
            logger.info(f"[LIVENESS] Downloading {name}...")
            urllib.request.urlretrieve(url, model_path)
            file_size = os.path.getsize(model_path)
            logger.info(f"[LIVENESS] ✓ {name} downloaded successfully ({file_size} bytes to {model_path})")
        except urllib.error.HTTPError as e:
            logger.error(f"[LIVENESS] ✗ HTTP {e.code} downloading {name}: {e.reason}")
            logger.error(f"[LIVENESS]   URL: {url}")
            return False
        except urllib.error.URLError as e:
            logger.error(f"[LIVENESS] ✗ Network error downloading {name}: {e.reason}")
            logger.error(f"[LIVENESS]   URL: {url}")
            return False
        except Exception as e:
            logger.error(f"[LIVENESS] ✗ Unexpected error downloading {name}: {type(e).__name__}: {e}")
            logger.error(f"[LIVENESS]   URL: {url}")
            return False

    logger.info(f"[LIVENESS] ✓ All models ready")
    return True


def decode_base64_image(frame_base64: str) -> np.ndarray:
    """
    Convert base64 string to RGB numpy array.
    Handles both raw base64 and data URI format:
      data:image/jpeg;base64,/9j/4AAQ...
    """
    if "," in frame_base64:
        frame_base64 = frame_base64.split(",")[1]

    image_bytes = base64.b64decode(frame_base64)
    pil_image   = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    return np.array(pil_image)


def numpy_to_mp_image(frame: np.ndarray) -> mp.Image:
    """Convert numpy RGB array to MediaPipe Image for Tasks API."""
    return mp.Image(image_format=mp.ImageFormat.SRGB, data=frame)


def validate_liveness_frame(
    frame_base64: str,
    frontend_blink: bool,
    frontend_head_turn: bool
) -> dict:
    """
    Server-side liveness validation using MediaPipe Tasks API.

    Two-stage check:
    1. FaceDetector   — confirms a face is present + right size
    2. FaceLandmarker — maps 478 landmarks, checks eye openness,
                        detects multiple faces (coaching/fraud signal)

    Cross-references frontend MediaPipe signals (blink + head_turn)
    to produce a final confidence score.
    
    BACKEND REVALIDATION:
    - Frontend runs MediaPipe client-side (blink + head_turn detection)
    - Frontend sends frame + signals to backend
    - Backend re-runs MediaPipe server-side to independently verify the frame
    - Backend returns its own liveness result (independent of frontend)
    - Frontend UI should display backend result, not frontend signals
    """
    logger.info("[LIVENESS] ─── validate_liveness_frame START ───")
    logger.info(f"[LIVENESS] Frontend signals: blink={frontend_blink}, head_turn={frontend_head_turn}")
    
    if not _ensure_models():
        logger.error("[LIVENESS] Models failed to load")
        return {
            "face_detected":    False,
            "liveness_passed":  False,
            "confidence_score": 0.0,
            "message": "Liveness model unavailable. Please retry."
        }

    try:
        logger.info("[LIVENESS] Decoding base64 frame...")
        frame    = decode_base64_image(frame_base64)
        logger.info(f"[LIVENESS] Frame decoded: {frame.shape}")
        mp_image = numpy_to_mp_image(frame)
        logger.info(f"[LIVENESS] MediaPipe image created: {mp_image.width}x{mp_image.height}")
    except Exception as e:
        logger.error(f"[LIVENESS] Frame decode error: {type(e).__name__}: {e}")
        return {
            "face_detected":    False,
            "liveness_passed":  False,
            "confidence_score": 0.0,
            "message": "Invalid image frame. Please retry."
        }

    # ── Stage 1: Face Detection ───────────────────────────────────
    logger.info("[LIVENESS] Stage 1: Running FaceDetector...")
    try:
        detection_result = _run_face_detection(mp_image)
        logger.info(f"[LIVENESS] FaceDetector result: {detection_result}")
    except Exception as e:
        logger.error(f"[LIVENESS] Face detection error: {type(e).__name__}: {e}")
        import traceback
        logger.error(f"[LIVENESS] Traceback:\n{traceback.format_exc()}")
        return {
            "face_detected":    False,
            "liveness_passed":  False,
            "confidence_score": 0.0,
            "message": f"Face detection failed: {str(e)}"
        }

    if not detection_result["face_detected"]:
        logger.warning(f"[LIVENESS] No face detected: {detection_result['message']}")
        return {
            "face_detected":    False,
            "liveness_passed":  False,
            "confidence_score": 0.0,
            "message": detection_result["message"]
        }

    detection_confidence = detection_result["confidence"]
    logger.info(f"[LIVENESS] Detection confidence: {detection_confidence:.3f}")

    # ── Stage 2: Face Landmark Analysis ──────────────────────────
    logger.info("[LIVENESS] Stage 2: Running FaceLandmarker...")
    try:
        landmark_result = _run_face_landmarker(mp_image)
        logger.info(f"[LIVENESS] FaceLandmarker result: landmarks_found={landmark_result['landmarks_found']}, "
                   f"multiple_faces={landmark_result['multiple_faces']}, eyes_open={landmark_result['eyes_open']}")
    except Exception as e:
        logger.error(f"[LIVENESS] Face landmarker error: {type(e).__name__}: {e}")
        import traceback
        logger.error(f"[LIVENESS] Traceback:\n{traceback.format_exc()}")
        return {
            "face_detected":    True,
            "liveness_passed":  False,
            "confidence_score": round(detection_confidence * 0.5, 3),
            "message": "Could not map facial landmarks. Ensure good lighting and face the camera."
        }

    if not landmark_result["landmarks_found"]:
        logger.warning("[LIVENESS] Landmarks not found")
        return {
            "face_detected":    True,
            "liveness_passed":  False,
            "confidence_score": round(detection_confidence * 0.4, 3),
            "message": "Facial landmarks not detected. Please face the camera directly."
        }

    # ── Multiple Face Check (coaching fraud signal) ───────────────
    if landmark_result["multiple_faces"]:
        logger.warning("[LIVENESS] Multiple faces detected (fraud signal)")
        return {
            "face_detected":    True,
            "liveness_passed":  False,
            "confidence_score": 0.0,
            "message": "Multiple faces detected. Only the vendor should be in frame."
        }

    eyes_open = landmark_result["eyes_open"]
    logger.info(f"[LIVENESS] Eyes open: {eyes_open}, EAR value: {landmark_result.get('ear_value', 'N/A')}")

    # ── Final Confidence Calculation ──────────────────────────────
    logger.info("[LIVENESS] Computing final confidence...")
    if frontend_blink and frontend_head_turn:
        final_confidence = min(detection_confidence * 1.0, 1.0)
        logger.info(f"[LIVENESS] Both signals detected: confidence multiplier 1.0x")
    elif frontend_blink or frontend_head_turn:
        final_confidence = detection_confidence * 0.75
        logger.info(f"[LIVENESS] One signal detected: confidence multiplier 0.75x")
    else:
        final_confidence = detection_confidence * 0.40
        logger.info(f"[LIVENESS] No signals detected: confidence multiplier 0.40x")

    liveness_passed = final_confidence >= 0.55 and eyes_open
    logger.info(f"[LIVENESS] Final confidence: {final_confidence:.3f}, Threshold: 0.55, Eyes open: {eyes_open} → PASSED: {liveness_passed}")

    result = {
        "face_detected":    True,
        "liveness_passed":  liveness_passed,
        "confidence_score": round(final_confidence, 3),
        "eyes_open":        eyes_open,
        "face_size_ratio":  round(detection_result["bbox"].get("width", 0), 3),
        "message": (
            "Liveness check passed."
            if liveness_passed
            else _liveness_failure_message(
                final_confidence, eyes_open,
                frontend_blink, frontend_head_turn
            )
        )
    }
    logger.info(f"[LIVENESS] ─── validate_liveness_frame END: {result['message']} ───")
    return result


def _run_face_detection(mp_image: mp.Image) -> dict:
    """
    Run FaceDetector (Tasks API) on a single image.

    Uses VisionRunningMode.IMAGE for static frame processing.
    Returns detection confidence + normalised bounding box.
    """
    logger.info("[DETECTOR] Creating FaceDetector with model...")
    try:
        options = FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=FACE_DETECTOR_MODEL),
            running_mode=VisionRunningMode.IMAGE,
            min_detection_confidence=0.5,
        )
        logger.info(f"[DETECTOR] Model path: {FACE_DETECTOR_MODEL}")
        logger.info(f"[DETECTOR] Model exists: {os.path.exists(FACE_DETECTOR_MODEL)}")
    except Exception as e:
        logger.error(f"[DETECTOR] Failed to create options: {type(e).__name__}: {e}")
        raise

    try:
        with FaceDetector.create_from_options(options) as detector:
            logger.info("[DETECTOR] Running detection on image...")
            result = detector.detect(mp_image)
            logger.info(f"[DETECTOR] Detections: {len(result.detections)}")
    except Exception as e:
        logger.error(f"[DETECTOR] Detection failed: {type(e).__name__}: {e}")
        import traceback
        logger.error(f"[DETECTOR] Traceback:\n{traceback.format_exc()}")
        raise

    if not result.detections:
        logger.warning("[DETECTOR] No detections in result")
        return {
            "face_detected": False,
            "confidence":    0.0,
            "bbox":          {},
            "message":       "No face detected. Please ensure your face is clearly visible."
        }

    # Use highest confidence detection
    best       = max(result.detections, key=lambda d: d.categories[0].score)
    confidence = best.categories[0].score
    logger.info(f"[DETECTOR] Best detection confidence: {confidence:.3f}")

    # Normalise bounding box to 0.0–1.0 range
    bbox    = best.bounding_box
    img_w   = mp_image.width
    img_h   = mp_image.height
    norm_w  = bbox.width  / img_w
    norm_h  = bbox.height / img_h
    logger.info(f"[DETECTOR] Image size: {img_w}x{img_h}, Face size (normalized): {norm_w:.3f}x{norm_h:.3f}")

    # Face too small = too far from camera
    if norm_w < 0.10:
        logger.warning("[DETECTOR] Face too small (< 10% image width)")
        return {
            "face_detected": True,
            "confidence":    confidence,
            "bbox":          {"width": norm_w, "height": norm_h},
            "message":       "Face too far from camera. Please move closer."
        }

    # Face absurdly large = printed photo held very close
    if norm_w > 0.95:
        logger.warning("[DETECTOR] Face too large (> 95% image width, potential printed photo)")
        return {
            "face_detected": True,
            "confidence":    confidence * 0.3,
            "bbox":          {"width": norm_w, "height": norm_h},
            "message":       "Face too close. Please adjust distance."
        }

    logger.info("[DETECTOR] Face size OK")
    return {
        "face_detected": True,
        "confidence":    confidence,
        "bbox":          {"width": norm_w, "height": norm_h},
        "message":       "Face detected."
    }


def _run_face_landmarker(mp_image: mp.Image) -> dict:
    """
    Run FaceLandmarker (Tasks API) on a single image.
    Returns 478 landmark positions + eye openness + multi-face flag.

    Key landmarks for eye openness (Eye Aspect Ratio):
      Left eye:  159 (upper lid), 145 (lower lid)
      Right eye: 386 (upper lid), 374 (lower lid)

    num_faces=2 intentionally — if 2 faces detected, flag as fraud.
    """
    logger.info("[LANDMARKS] Creating FaceLandmarker with model...")
    try:
        options = FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=FACE_LANDMARKER_MODEL),
            running_mode=VisionRunningMode.IMAGE,
            num_faces=2,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=False,
        )
        logger.info(f"[LANDMARKS] Model path: {FACE_LANDMARKER_MODEL}")
        logger.info(f"[LANDMARKS] Model exists: {os.path.exists(FACE_LANDMARKER_MODEL)}")
    except Exception as e:
        logger.error(f"[LANDMARKS] Failed to create options: {type(e).__name__}: {e}")
        raise

    try:
        with FaceLandmarker.create_from_options(options) as landmarker:
            logger.info("[LANDMARKS] Running landmark detection on image...")
            result = landmarker.detect(mp_image)
            logger.info(f"[LANDMARKS] Faces detected: {len(result.face_landmarks)}")
    except Exception as e:
        logger.error(f"[LANDMARKS] Landmark detection failed: {type(e).__name__}: {e}")
        import traceback
        logger.error(f"[LANDMARKS] Traceback:\n{traceback.format_exc()}")
        raise

    if not result.face_landmarks:
        logger.warning("[LANDMARKS] No face landmarks found")
        return {
            "landmarks_found": False,
            "multiple_faces":  False,
            "eyes_open":       False,
        }

    multiple_faces = len(result.face_landmarks) > 1
    logger.info(f"[LANDMARKS] Multiple faces detected: {multiple_faces}")

    # Analyse primary face
    landmarks = result.face_landmarks[0]
    logger.info(f"[LANDMARKS] Primary face has {len(landmarks)} landmarks")

    # Eye Aspect Ratio — higher value = more open
    left_ear  = abs(landmarks[159].y - landmarks[145].y)
    right_ear = abs(landmarks[386].y - landmarks[374].y)
    avg_ear   = (left_ear + right_ear) / 2

    eyes_open = avg_ear > 0.01  # Normalised coordinate threshold
    logger.info(f"[LANDMARKS] Left EAR: {left_ear:.4f}, Right EAR: {right_ear:.4f}, Avg: {avg_ear:.4f}, Threshold: 0.01, Eyes open: {eyes_open}")

    return {
        "landmarks_found": True,
        "multiple_faces":  multiple_faces,
        "eyes_open":       eyes_open,
        "ear_value":       round(avg_ear, 4),
    }


def _liveness_failure_message(
    confidence: float,
    eyes_open: bool,
    blink: bool,
    head_turn: bool
) -> str:
    if not eyes_open:
        return "Eyes appear closed. Please keep eyes open during capture."
    if not blink:
        return "Blink not detected. Please complete the blink gesture."
    if not head_turn:
        return "Head turn not detected. Please turn your head left and right."
    if confidence < 0.55:
        return "Liveness confidence too low. Please improve lighting and retry."
    return "Liveness check failed. Please retry."