import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Score Weights ────────────────────────────────────────────────────────────
# These weights define how each signal contributes to the VaultScore.
# Adjustable as more real transaction data is collected.

WEIGHTS = {
    "identity":        0.40,   # NIN + face match (most critical)
    "liveness":        0.15,   # MediaPipe liveness confidence
    "voice":           0.10,   # AssemblyAI voice challenge
    "delivery_rate":   0.20,   # % of orders delivered successfully
    "dispute_rate":    0.10,   # % of orders with disputes (inverted)
    "recency":         0.05,   # Activity recency bonus
}

TRUST_LEVELS = [
    (85, "VERIFIED"),
    (65, "HIGH"),
    (45, "MEDIUM"),
    (0,  "LOW"),
]


def calculate_vault_score(
    identity_score: float,          # 0-100 from Youverify
    liveness_confidence: float,     # 0-1 from MediaPipe
    voice_score: float,             # 0-1 from AssemblyAI
    total_orders: int = 0,
    successful_deliveries: int = 0,
    total_disputes: int = 0,
    resolved_disputes: int = 0,
    days_since_last_order: Optional[int] = None
) -> dict:
    """
    VaultScore: Evolving vendor trust score (0-100).

    For new vendors (no transaction history):
    Score is based entirely on identity + liveness + voice.
    As they transact, delivery and dispute signals take over.

    Score Breakdown:
    - Identity (40%):  Youverify NIN + face match score
    - Liveness (15%):  MediaPipe confidence
    - Voice (10%):     AssemblyAI phrase match + no coaching
    - Delivery (20%):  Successful delivery rate
    - Disputes (10%):  Low dispute rate is rewarded
    - Recency (5%):    Active vendors score slightly higher
    """

    # ── Identity Signal ───────────────────────────────────────────
    identity_signal = identity_score / 100  # normalise to 0-1

    # ── Liveness Signal ───────────────────────────────────────────
    liveness_signal = max(0.0, min(liveness_confidence, 1.0))

    # ── Voice Signal ──────────────────────────────────────────────
    voice_signal = max(0.0, min(voice_score, 1.0))

    # ── Delivery Rate Signal ──────────────────────────────────────
    if total_orders > 0:
        delivery_signal = successful_deliveries / total_orders
    else:
        # New vendor: neutral score — don't penalise for no history
        delivery_signal = 0.50

    # ── Dispute Rate Signal (inverted — fewer disputes = higher score) ──
    if total_orders > 0:
        dispute_rate = total_disputes / total_orders
        # Resolved disputes soften the penalty
        resolution_bonus = (resolved_disputes / total_disputes * 0.30) if total_disputes > 0 else 0
        dispute_signal = max(0.0, 1.0 - dispute_rate + resolution_bonus)
    else:
        dispute_signal = 0.80  # New vendor: slight trust benefit of doubt

    # ── Recency Signal ────────────────────────────────────────────
    if days_since_last_order is None:
        recency_signal = 0.50  # New vendor
    elif days_since_last_order <= 7:
        recency_signal = 1.0   # Active this week
    elif days_since_last_order <= 30:
        recency_signal = 0.80  # Active this month
    elif days_since_last_order <= 90:
        recency_signal = 0.50  # Somewhat active
    else:
        recency_signal = 0.20  # Dormant — score decays

    # ── Weighted Score ────────────────────────────────────────────
    raw_score = (
        identity_signal  * WEIGHTS["identity"]
        + liveness_signal  * WEIGHTS["liveness"]
        + voice_signal     * WEIGHTS["voice"]
        + delivery_signal  * WEIGHTS["delivery_rate"]
        + dispute_signal   * WEIGHTS["dispute_rate"]
        + recency_signal   * WEIGHTS["recency"]
    )

    vault_score = round(raw_score * 100, 2)
    vault_score = max(0.0, min(100.0, vault_score))

    trust_level = _get_trust_level(vault_score)

    return {
        "vault_score": vault_score,
        "trust_level": trust_level,
        "verified": trust_level in ("HIGH", "VERIFIED"),
        "score_breakdown": {
            "identity": {
                "score": round(identity_signal * 100, 1),
                "weight": WEIGHTS["identity"],
                "contribution": round(identity_signal * WEIGHTS["identity"] * 100, 1)
            },
            "liveness": {
                "score": round(liveness_signal * 100, 1),
                "weight": WEIGHTS["liveness"],
                "contribution": round(liveness_signal * WEIGHTS["liveness"] * 100, 1)
            },
            "voice": {
                "score": round(voice_signal * 100, 1),
                "weight": WEIGHTS["voice"],
                "contribution": round(voice_signal * WEIGHTS["voice"] * 100, 1)
            },
            "delivery_rate": {
                "score": round(delivery_signal * 100, 1),
                "weight": WEIGHTS["delivery_rate"],
                "contribution": round(delivery_signal * WEIGHTS["delivery_rate"] * 100, 1)
            },
            "dispute_rate": {
                "score": round(dispute_signal * 100, 1),
                "weight": WEIGHTS["dispute_rate"],
                "contribution": round(dispute_signal * WEIGHTS["dispute_rate"] * 100, 1)
            },
            "recency": {
                "score": round(recency_signal * 100, 1),
                "weight": WEIGHTS["recency"],
                "contribution": round(recency_signal * WEIGHTS["recency"] * 100, 1)
            }
        }
    }


def _get_trust_level(score: float) -> str:
    for threshold, level in TRUST_LEVELS:
        if score >= threshold:
            return level
    return "LOW"
