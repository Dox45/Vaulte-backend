import math
import logging
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def calculate_distance_metres(
    lat1: float, lng1: float,
    lat2: float, lng2: float
) -> float:
    """
    Haversine formula — calculates distance between two GPS coordinates.
    Returns distance in metres.
    """
    R = 6_371_000  # Earth radius in metres

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


async def release_escrow(
    order_id: str,
    vendor_lat: float,
    vendor_lng: float,
    buyer_lat: float,
    buyer_lng: float,
    max_distance_metres: float = 500
) -> dict:
    """
    Verify GPS coordinates for delivery confirmation.

    GPS Verification:
    - Calculate distance between vendor's delivery GPS and buyer's receipt GPS
    - If within max_distance_metres (default 500m) → returns gps_verified=true
    - If outside range → returns gps_verified=false, order flagged for manual review
    
    NOTE: Escrow creation and release is now handled by the JS backend via Squad API.
    This function only verifies GPS and signals whether release should proceed.
    """

    # ── GPS Verification ──────────────────────────────────────────
    distance = calculate_distance_metres(
        vendor_lat, vendor_lng,
        buyer_lat, buyer_lng
    )

    gps_verified = distance <= max_distance_metres

    if not gps_verified:
        logger.warning(
            f"GPS mismatch for order {order_id}: "
            f"vendor and buyer are {distance:.0f}m apart (max {max_distance_metres}m)"
        )
        return {
            "success": True,
            "order_id": order_id,
            "gps_verified": False,
            "distance_metres": round(distance, 2),
            "message": (
                f"GPS mismatch detected. Vendor and buyer locations are "
                f"{distance:.0f}m apart. Order flagged for manual review."
            )
        }

    # ── GPS Verified ──────────────────────────────────────────────
    # JS backend will call Squad API to release escrow when gps_verified=true
    return {
        "success": True,
        "order_id": order_id,
        "gps_verified": True,
        "distance_metres": round(distance, 2),
        "message": (
            f"GPS verified. JS backend should now release escrow to vendor via Squad API."
        )
    }
