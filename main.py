from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers.vault import router
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

### Verification Pipeline
| Step | Endpoint | Technology |
|------|----------|------------|
| 1 | `POST /vendor/liveness` | MediaPipe (server-side frame validation) |
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
- Steps 1–3 must be called in sequence — each step gates the next
- The `session_id` must be consistent across all steps for one verification session
- The `frame_base64` from Step 1 is reused as `selfie_image` in Step 3
- AssemblyAI WebSocket connection is opened directly from the frontend using the token from Step 2a
    """,
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # Restrict to your frontend domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1", tags=["Vault"])

@app.get("/", tags=["Health"])
async def root():
    return {
        "service": "Vault API",
        "status": "running",
        "version": "1.0.0",
        "docs": "/docs"
    }

@app.get("/health", tags=["Health"])
async def health():
    return {"status": "healthy"}
