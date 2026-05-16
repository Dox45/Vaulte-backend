"""
Merchant Fraud & Anomaly Detection API
Real-time transaction anomaly detection using Isolation Forest, 
statistical methods, geo-velocity analysis, and behavioral profiling.
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import asyncio
import json
import random
import math
import time
from datetime import datetime, timedelta
from collections import defaultdict, deque
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
import warnings

warnings.filterwarnings("ignore")

app = FastAPI(title="Fraud Detection API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Merchant Profiles ────────────────────────────────────────────────────────

MERCHANTS = {
    "MRC-001": {
        "name": "Lagos Fresh Market",
        "category": "Grocery",
        "avg_txn": 4500,
        "avg_daily_vol": 85,
        "location": {"lat": 6.4550, "lon": 3.3841, "city": "Lagos"},
        "typical_hours": (7, 21),
        "currency": "NGN",
    },
    "MRC-002": {
        "name": "TechHub Electronics",
        "category": "Electronics",
        "avg_txn": 125000,
        "avg_daily_vol": 22,
        "location": {"lat": 6.6018, "lon": 3.3515, "city": "Ikeja"},
        "typical_hours": (9, 19),
        "currency": "NGN",
    },
    "MRC-003": {
        "name": "Abuja Prime Pharmacy",
        "category": "Pharmacy",
        "avg_txn": 8200,
        "avg_daily_vol": 120,
        "location": {"lat": 9.0579, "lon": 7.4951, "city": "Abuja"},
        "typical_hours": (8, 22),
        "currency": "NGN",
    },
    "MRC-004": {
        "name": "PortHarcourt AutoParts",
        "category": "Automotive",
        "avg_txn": 45000,
        "avg_daily_vol": 18,
        "location": {"lat": 4.8156, "lon": 7.0498, "city": "Port Harcourt"},
        "typical_hours": (8, 18),
        "currency": "NGN",
    },
}

# ─── Geo-locations pool for simulation ───────────────────────────────────────

GEO_POOL = [
    {"lat": 6.4550, "lon": 3.3841, "city": "Lagos", "country": "NG", "risk": 0.1},
    {"lat": 9.0579, "lon": 7.4951, "city": "Abuja", "country": "NG", "risk": 0.1},
    {"lat": 4.8156, "lon": 7.0498, "city": "Port Harcourt", "country": "NG", "risk": 0.15},
    {"lat": 6.3350, "lon": 5.6037, "city": "Benin City", "country": "NG", "risk": 0.12},
    {"lat": 51.5074, "lon": -0.1278, "city": "London", "country": "GB", "risk": 0.45},
    {"lat": 40.7128, "lon": -74.0060, "city": "New York", "country": "US", "risk": 0.48},
    {"lat": 1.3521, "lon": 103.8198, "city": "Singapore", "country": "SG", "risk": 0.55},
    {"lat": 55.7558, "lon": 37.6176, "city": "Moscow", "country": "RU", "risk": 0.72},
    {"lat": 22.3193, "lon": 114.1694, "city": "Hong Kong", "country": "HK", "risk": 0.62},
    {"lat": 25.2048, "lon": 55.2708, "city": "Dubai", "country": "AE", "risk": 0.38},
]

# ─── In-memory State ──────────────────────────────────────────────────────────

class TransactionStore:
    def __init__(self):
        self.transactions = deque(maxlen=5000)
        self.merchant_windows = defaultdict(lambda: deque(maxlen=500))
        self.card_history = defaultdict(lambda: deque(maxlen=100))
        self.merchant_baselines = {}
        self.model = None
        self.scaler = StandardScaler()
        self.training_data = []
        self._build_baseline_model()

    def _build_baseline_model(self):
        """Pre-train isolation forest on synthetic normal data"""
        np.random.seed(42)
        normal_samples = []
        for _ in range(2000):
            normal_samples.append([
                np.random.lognormal(9, 0.8),     # amount
                np.random.randint(8, 20),         # hour
                np.random.uniform(0, 1),          # velocity_score
                np.random.uniform(0, 0.3),        # geo_risk
                np.random.exponential(0.5),       # amount_deviation
                np.random.uniform(0, 0.2),        # freq_anomaly
                np.random.uniform(0, 0.1),        # round_number_score
                np.random.randint(1, 5),          # txn_per_hour
            ])
        X = np.array(normal_samples)
        self.scaler.fit(X)
        X_scaled = self.scaler.transform(X)
        self.model = IsolationForest(
            n_estimators=200,
            contamination=0.08,
            random_state=42,
            max_features=0.8,
        )
        self.model.fit(X_scaled)

store = TransactionStore()

# ─── Anomaly Detection Engine ─────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def compute_velocity_score(card_id: str, new_geo: dict, timestamp: float) -> dict:
    history = store.card_history[card_id]
    if len(history) < 1:
        return {"score": 0.0, "km_per_hour": 0, "impossible": False}
    last = history[-1]
    dt_hours = max((timestamp - last["ts"]) / 3600, 0.001)
    dist_km = haversine_km(last["lat"], last["lon"], new_geo["lat"], new_geo["lon"])
    kmph = dist_km / dt_hours
    impossible = kmph > 900  # faster than commercial flight
    score = min(kmph / 1500, 1.0)
    return {"score": score, "km_per_hour": round(kmph, 1), "impossible": impossible, "distance_km": round(dist_km, 1)}

def compute_amount_deviation(merchant_id: str, amount: float) -> float:
    merchant = MERCHANTS.get(merchant_id, {})
    avg = merchant.get("avg_txn", 10000)
    std = avg * 0.6
    z = abs((amount - avg) / std)
    return min(z / 5.0, 1.0)

def compute_frequency_anomaly(merchant_id: str, window_seconds: int = 3600) -> dict:
    now = time.time()
    window = store.merchant_windows[merchant_id]
    recent = [t for t in window if now - t["ts"] < window_seconds]
    count = len(recent)
    merchant = MERCHANTS.get(merchant_id, {})
    avg_hourly = merchant.get("avg_daily_vol", 50) / 12  # daily / 12 two-hour windows
    ratio = count / max(avg_hourly, 1)
    return {
        "count_last_hour": count,
        "expected_hourly": round(avg_hourly, 1),
        "ratio": round(ratio, 2),
        "score": min(max(ratio - 1, 0) / 3.0, 1.0),
    }

def is_round_number(amount: float) -> float:
    """High round numbers can signal structuring"""
    modulos = [1000, 5000, 10000, 50000, 100000]
    for mod in modulos:
        if amount % mod == 0 and amount >= mod:
            return min(math.log10(amount / mod + 1) / 2, 1.0)
    return 0.0

def is_off_hours(merchant_id: str, hour: int) -> bool:
    merchant = MERCHANTS.get(merchant_id, {})
    start, end = merchant.get("typical_hours", (8, 20))
    return hour < start or hour > end

def analyze_transaction(txn: dict) -> dict:
    merchant_id = txn["merchant_id"]
    card_id = txn["card_id"]
    amount = txn["amount"]
    geo = txn["geo"]
    ts = txn["timestamp"]
    hour = datetime.fromtimestamp(ts).hour

    # Individual signals
    velocity = compute_velocity_score(card_id, geo, ts)
    amount_dev = compute_amount_deviation(merchant_id, amount)
    freq = compute_frequency_anomaly(merchant_id)
    round_score = is_round_number(amount)
    off_hours = is_off_hours(merchant_id, hour)
    geo_risk = geo.get("risk", 0.1)

    # ML Feature vector
    features = np.array([[
        amount,
        hour,
        velocity["score"],
        geo_risk,
        amount_dev,
        freq["score"],
        round_score,
        freq["count_last_hour"],
    ]])
    features_scaled = store.scaler.transform(features)
    if_score = store.model.score_samples(features_scaled)[0]
    # Isolation forest: more negative = more anomalous; normalize to [0,1]
    ml_anomaly_score = max(0, min((-if_score - 0.1) / 0.4, 1.0))

    # Weighted composite risk score
    weights = {
        "velocity": 0.20,
        "amount_deviation": 0.18,
        "frequency": 0.18,
        "geo_risk": 0.15,
        "ml_score": 0.17,
        "off_hours": 0.07,
        "round_number": 0.05,
    }
    signals = {
        "velocity": velocity["score"],
        "amount_deviation": amount_dev,
        "frequency": freq["score"],
        "geo_risk": geo_risk,
        "ml_score": ml_anomaly_score,
        "off_hours": 1.0 if off_hours else 0.0,
        "round_number": round_score,
    }
    composite = sum(signals[k] * weights[k] for k in weights)

    # Determine flags
    flags = []
    if velocity["impossible"]: flags.append("IMPOSSIBLE_VELOCITY")
    if velocity["score"] > 0.6: flags.append("HIGH_VELOCITY")
    if amount_dev > 0.7: flags.append("UNUSUAL_AMOUNT")
    if freq["ratio"] > 2.5: flags.append("VOLUME_SPIKE")
    if off_hours: flags.append("OFF_HOURS")
    if geo_risk > 0.5: flags.append("HIGH_RISK_GEO")
    if round_score > 0.5: flags.append("STRUCTURED_AMOUNT")
    if ml_anomaly_score > 0.65: flags.append("ML_ANOMALY")

    # Risk tier
    if composite >= 0.70:
        risk_tier = "CRITICAL"
    elif composite >= 0.50:
        risk_tier = "HIGH"
    elif composite >= 0.30:
        risk_tier = "MEDIUM"
    else:
        risk_tier = "LOW"

    return {
        "risk_score": round(composite, 4),
        "risk_tier": risk_tier,
        "flags": flags,
        "signals": {k: round(v, 4) for k, v in signals.items()},
        "velocity_details": velocity,
        "frequency_details": freq,
        "ml_anomaly_score": round(ml_anomaly_score, 4),
        "off_hours": off_hours,
        "round_amount": round_score > 0.3,
    }

# ─── Transaction Generator (Simulation) ──────────────────────────────────────

CARD_POOL = [f"CARD-{i:04d}" for i in range(1, 201)]
txn_counter = [0]

def generate_transaction(force_fraud: bool = False) -> dict:
    merchant_id = random.choice(list(MERCHANTS.keys()))
    merchant = MERCHANTS[merchant_id]
    card_id = random.choice(CARD_POOL)
    ts = time.time()
    now_dt = datetime.now()

    if force_fraud:
        scenario = random.choice(["smurfing", "velocity", "geo_jump", "volume_spike", "off_hours_large"])
        
        if scenario == "smurfing":
            amount = random.choice([4999, 9999, 49999, 99999]) + random.uniform(0, 50)
        elif scenario == "velocity":
            amount = merchant["avg_txn"] * random.uniform(0.8, 1.2)
        elif scenario == "geo_jump":
            amount = merchant["avg_txn"] * random.uniform(1.0, 3.0)
        elif scenario == "volume_spike":
            amount = merchant["avg_txn"] * random.uniform(0.5, 0.9)
        else:
            amount = merchant["avg_txn"] * random.uniform(5, 15)

        geo = random.choice([g for g in GEO_POOL if g["risk"] > 0.4])
        hour_offset = random.choice([-3, -2, 2, 3, 4])
        ts = ts + hour_offset * 3600
    else:
        base = merchant["avg_txn"]
        amount = max(100, np.random.lognormal(math.log(base), 0.5))
        geo_candidates = [g for g in GEO_POOL if g["city"] in [merchant["location"]["city"], "Lagos", "Abuja"]]
        geo = random.choice(geo_candidates if geo_candidates else GEO_POOL[:4])

    txn_counter[0] += 1
    txn_id = f"TXN-{txn_counter[0]:06d}"

    return {
        "id": txn_id,
        "merchant_id": merchant_id,
        "merchant_name": merchant["name"],
        "merchant_category": merchant["category"],
        "card_id": card_id,
        "amount": round(amount, 2),
        "currency": merchant["currency"],
        "timestamp": ts,
        "datetime": datetime.fromtimestamp(ts).isoformat(),
        "geo": geo,
    }

def process_and_store(txn: dict) -> dict:
    analysis = analyze_transaction(txn)
    enriched = {**txn, "analysis": analysis}

    # Update histories
    store.transactions.appendleft(enriched)
    store.merchant_windows[txn["merchant_id"]].append({"ts": txn["timestamp"], "amount": txn["amount"]})
    store.card_history[txn["card_id"]].append({"ts": txn["timestamp"], "lat": txn["geo"]["lat"], "lon": txn["geo"]["lon"]})

    return enriched

# ─── Metrics Computation ──────────────────────────────────────────────────────

def compute_dashboard_metrics() -> dict:
    txns = list(store.transactions)
    if not txns:
        return {}

    total = len(txns)
    flagged = [t for t in txns if t["analysis"]["risk_tier"] in ("HIGH", "CRITICAL")]
    critical = [t for t in txns if t["analysis"]["risk_tier"] == "CRITICAL"]

    amounts = [t["amount"] for t in txns]
    flagged_amounts = [t["amount"] for t in flagged]

    # Volume by merchant
    merchant_vol = defaultdict(lambda: {"count": 0, "amount": 0, "flagged": 0})
    for t in txns:
        mid = t["merchant_id"]
        merchant_vol[mid]["count"] += 1
        merchant_vol[mid]["amount"] += t["amount"]
        if t["analysis"]["risk_tier"] in ("HIGH", "CRITICAL"):
            merchant_vol[mid]["flagged"] += 1

    # Flag frequency
    flag_counts = defaultdict(int)
    for t in flagged:
        for f in t["analysis"]["flags"]:
            flag_counts[f] += 1

    # Hourly distribution (last 24h of simulated data)
    hourly = defaultdict(lambda: {"total": 0, "flagged": 0})
    for t in txns[:200]:
        h = datetime.fromtimestamp(t["timestamp"]).hour
        hourly[h]["total"] += 1
        if t["analysis"]["risk_tier"] in ("HIGH", "CRITICAL"):
            hourly[h]["flagged"] += 1

    return {
        "total_transactions": total,
        "flagged_count": len(flagged),
        "critical_count": len(critical),
        "flag_rate": round(len(flagged) / max(total, 1), 4),
        "total_volume": round(sum(amounts), 2),
        "flagged_volume": round(sum(flagged_amounts), 2),
        "avg_risk_score": round(sum(t["analysis"]["risk_score"] for t in txns) / max(total, 1), 4),
        "merchant_summary": {
            mid: {
                **data,
                "name": MERCHANTS.get(mid, {}).get("name", mid),
                "amount": round(data["amount"], 2),
            }
            for mid, data in merchant_vol.items()
        },
        "flag_frequency": dict(flag_counts),
        "hourly_distribution": {str(h): v for h, v in hourly.items()},
        "top_flagged": [
            {
                "id": t["id"],
                "merchant_name": t["merchant_name"],
                "amount": t["amount"],
                "risk_score": t["analysis"]["risk_score"],
                "risk_tier": t["analysis"]["risk_tier"],
                "flags": t["analysis"]["flags"],
                "datetime": t["datetime"],
                "geo": t["geo"],
                "card_id": t["card_id"],
            }
            for t in sorted(flagged, key=lambda x: x["analysis"]["risk_score"], reverse=True)[:20]
        ],
    }

# ─── REST Endpoints ───────────────────────────────────────────────────────────

class TransactionInput(BaseModel):
    merchant_id: str
    card_id: Optional[str] = None
    amount: float
    geo_city: Optional[str] = "Lagos"

@app.get("/")
def root():
    return {"service": "Fraud Detection API", "status": "operational", "version": "1.0.0"}

@app.get("/metrics")
def get_metrics():
    return compute_dashboard_metrics()

@app.get("/transactions")
def get_transactions(limit: int = 50, risk_tier: Optional[str] = None):
    txns = list(store.transactions)
    if risk_tier:
        txns = [t for t in txns if t["analysis"]["risk_tier"] == risk_tier.upper()]
    return {"transactions": txns[:limit], "total": len(txns)}

@app.post("/analyze")
def analyze_manual(txn: TransactionInput):
    geo = next((g for g in GEO_POOL if g["city"] == txn.geo_city), GEO_POOL[0])
    card = txn.card_id or random.choice(CARD_POOL)
    raw = {
        "id": f"MANUAL-{int(time.time())}",
        "merchant_id": txn.merchant_id,
        "merchant_name": MERCHANTS.get(txn.merchant_id, {}).get("name", "Unknown"),
        "merchant_category": MERCHANTS.get(txn.merchant_id, {}).get("category", "Unknown"),
        "card_id": card,
        "amount": txn.amount,
        "currency": "NGN",
        "timestamp": time.time(),
        "datetime": datetime.now().isoformat(),
        "geo": geo,
    }
    return process_and_store(raw)

@app.post("/simulate/burst")
def simulate_burst(count: int = 20, fraud_ratio: float = 0.4):
    """Inject a burst of transactions for demo purposes"""
    results = []
    for i in range(min(count, 100)):
        is_fraud = random.random() < fraud_ratio
        txn = generate_transaction(force_fraud=is_fraud)
        results.append(process_and_store(txn))
    return {"injected": len(results), "flagged": sum(1 for r in results if r["analysis"]["risk_tier"] in ("HIGH", "CRITICAL"))}

@app.get("/merchants")
def get_merchants():
    return MERCHANTS

# ─── WebSocket – Live Stream ──────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)

manager = ConnectionManager()

@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Generate 1-3 transactions per tick
            batch_size = random.randint(1, 3)
            fraud_prob = 0.18  # ~18% baseline fraud injection
            for _ in range(batch_size):
                is_fraud = random.random() < fraud_prob
                txn = generate_transaction(force_fraud=is_fraud)
                enriched = process_and_store(txn)
                await manager.broadcast({
                    "type": "transaction",
                    "data": enriched,
                    "metrics_summary": {
                        "total": len(store.transactions),
                        "flagged": sum(1 for t in store.transactions if t["analysis"]["risk_tier"] in ("HIGH","CRITICAL")),
                        "critical": sum(1 for t in store.transactions if t["analysis"]["risk_tier"] == "CRITICAL"),
                    }
                })
            await asyncio.sleep(random.uniform(0.8, 2.2))
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    # Pre-populate with some transactions
    print("🔧 Pre-populating transaction history...")
    for i in range(150):
        is_fraud = random.random() < 0.15
        txn = generate_transaction(force_fraud=is_fraud)
        process_and_store(txn)
    print(f"✅ Loaded {len(store.transactions)} baseline transactions")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")