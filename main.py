from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers.vault import router as vault_router
from app.routers.liveness import router as liveness_router
from app.core.redis import redis_lifespan
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s"
)

app = FastAPI(
    title="Vault API",
    description="""
## Vault — Retail Infrastructure for Ecom Brands

AI-powered vendor verification and escrow system.

### Verification Pipeline (Hardened)
| Step | Endpoint | Technology |
|------|----------|------------|
| 1a | `GET /vendor/liveness/challenge` | Server-driven challenge generation |
| 1b | `POST /vendor/liveness` | Timing + Entropy + Challenge verification |
| 2a | `POST /vendor/voice/start` | AssemblyAI (get real-time token) |
| 2b | `POST /vendor/voice/verify` | AssemblyAI (verify transcript) |
| 3 | `POST /vendor/verify-identity` | Youverify (NIN + face match) |
| 4 | `GET /vendor/score/{vendor_id}` | VaultScore |

### Escrow Pipeline
| Step | Endpoint | Technology |
|------|----------|------------|
| 5 | `POST /order/create` | Squad Virtual Account |
| 6 | `POST /order/confirm-delivery` | GPS check + Squad Transfer |

### Frontend Developer Notes
- Steps 1a & 1b are the new hardened liveness flow.
- The `session_id` must be consistent across all steps.
- Submission must include the `nonce` and `sequence` from the challenge.
    """,
    version="1.1.0",
    lifespan=redis_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(liveness_router, prefix="/api/v1", tags=["Liveness"])
app.include_router(vault_router, prefix="/api/v1", tags=["Vault"])

@app.get("/", tags=["Health"])
async def root():
    return {
        "service": "Vault API",
        "status": "running",
        "version": "1.1.0",
        "docs": "/docs"
    }

@app.get("/health", tags=["Health"])
async def health():
    return {"status": "healthy"}
