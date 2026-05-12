# Vault API

Retail infrastructure for ecom brands. AI-powered vendor verification + Squad escrow.

## Stack
- **FastAPI** — Python backend
- **MediaPipe** — Server-side liveness frame validation
- **AssemblyAI** — Real-time voice challenge
- **Youverify** — NIN + face match against NIMC database
- **Squad API** — Escrow via virtual accounts + disbursement

---

## Setup

```bash
# 1. Clone and install
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Fill in your API keys in .env

# 3. Run
uvicorn main:app --reload

# 4. View auto-generated API docs
open http://localhost:8000/docs
```

---

## Environment Variables

```env
YOUVERIFY_TOKEN=        # From os.youverify.co → Account Settings → API/Webhooks
YOUVERIFY_BASE_URL=     # https://api.sandbox.youverify.co (sandbox)

ASSEMBLYAI_API_KEY=     # From assemblyai.com dashboard

SQUAD_SECRET_KEY=       # From squadco.com sandbox dashboard
SQUAD_BASE_URL=         # https://sandbox-api-d.squadco.com (sandbox)
```

---

## API Endpoints

Base URL: `http://localhost:8000/api/v1`

### Verification Pipeline (must call in order)

#### Step 1 — Liveness Check
```
POST /vendor/liveness
```
```json
{
  "session_id": "unique-session-uuid",
  "frame_base64": "data:image/jpeg;base64,/9j/4AAQ...",
  "blink_detected": true,
  "head_turn_detected": true
}
```

#### Step 2a — Start Voice Challenge
```
POST /vendor/voice/start
```
```json
{ "session_id": "unique-session-uuid" }
```
Returns `challenge_phrase` + `websocket_url` for frontend to connect to AssemblyAI directly.

#### Step 2b — Verify Voice
```
POST /vendor/voice/verify
```
```json
{
  "session_id": "unique-session-uuid",
  "transcript": "I am verifying my Vault account today",
  "audio_confidence": 0.92,
  "multiple_speakers_detected": false
}
```

#### Step 3 — Identity Verification
```
POST /vendor/verify-identity
```
```json
{
  "session_id": "unique-session-uuid",
  "nin": "12345678901",
  "first_name": "Sarah",
  "last_name": "Doe",
  "date_of_birth": "1988-04-04",
  "selfie_image": "data:image/jpeg;base64,/9j/4AAQ..."
}
```
> `selfie_image` should be the same frame captured in Step 1.

#### Get VaultScore
```
GET /vendor/score/{vendor_id}
```

---

### Escrow Pipeline

#### Step 4 — Create Order
```
POST /order/create
```
```json
{
  "vendor_id": "vendor-uuid",
  "buyer_email": "buyer@example.com",
  "amount": 15000,
  "product_description": "Blue Ankara fabric 6 yards"
}
```
Returns `virtual_account_number` for buyer to pay into.

#### Step 5 — Confirm Delivery
```
POST /order/confirm-delivery
```
```json
{
  "order_id": "VAULT-ABC123",
  "vendor_lat": 6.5244,
  "vendor_lng": 3.3792,
  "buyer_lat": 6.5250,
  "buyer_lng": 3.3800
}
```
GPS verified within 500m → escrow released to vendor automatically.

---

## Frontend Developer Guide

### MediaPipe Setup (Browser)
```html
<script src="https://cdn.jsdelivr.net/npm/@mediapipe/face_mesh/face_mesh.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@mediapipe/camera_utils/camera_utils.js"></script>
```

```javascript
// 1. Init MediaPipe FaceMesh
const faceMesh = new FaceMesh({ locateFile: (file) =>
  `https://cdn.jsdelivr.net/npm/@mediapipe/face_mesh/${file}`
})

faceMesh.setOptions({
  maxNumFaces: 1,
  refineLandmarks: true,
  minDetectionConfidence: 0.6,
  minTrackingConfidence: 0.6
})

// 2. Detect blink using eye landmarks
// Left eye: landmarks 159 (upper) and 145 (lower)
// Right eye: landmarks 386 (upper) and 374 (lower)
function detectBlink(landmarks) {
  const leftEAR = Math.abs(landmarks[159].y - landmarks[145].y)
  const rightEAR = Math.abs(landmarks[386].y - landmarks[374].y)
  return (leftEAR + rightEAR) / 2 < 0.015  // Eyes closed threshold
}

// 3. Detect head turn using nose tip (landmark 1) vs face center
function detectHeadTurn(landmarks) {
  const noseTip = landmarks[1]
  return Math.abs(noseTip.x - 0.5) > 0.08  // Turned if nose is off-center
}

// 4. Capture frame as base64 when gestures complete
function captureFrame(videoElement) {
  const canvas = document.createElement('canvas')
  canvas.width = videoElement.videoWidth
  canvas.height = videoElement.videoHeight
  canvas.getContext('2d').drawImage(videoElement, 0, 0)
  return canvas.toDataURL('image/jpeg', 0.8)
}
```

### AssemblyAI Real-Time Setup (Browser)
```javascript
// After calling POST /vendor/voice/start
// Connect to the returned websocket_url

async function startVoiceChallenge(sessionId) {
  // 1. Get token from your backend
  const { challenge_phrase, websocket_url } = await fetch('/api/v1/vendor/voice/start', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId })
  }).then(r => r.json())

  // 2. Show phrase to vendor
  showChallengePhrase(challenge_phrase)

  // 3. Connect to AssemblyAI WebSocket
  const socket = new WebSocket(websocket_url)
  const mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true })
  const mediaRecorder = new MediaRecorder(mediaStream, { mimeType: 'audio/webm' })

  let finalTranscript = ''
  let audioConfidence = 0

  socket.onmessage = (event) => {
    const data = JSON.parse(event.data)
    if (data.message_type === 'FinalTranscript') {
      finalTranscript = data.text
      audioConfidence = data.confidence
    }
  }

  mediaRecorder.ondataavailable = (event) => {
    if (socket.readyState === WebSocket.OPEN) {
      const reader = new FileReader()
      reader.onload = () => {
        const base64 = reader.result.split(',')[1]
        socket.send(JSON.stringify({ audio_data: base64 }))
      }
      reader.readAsDataURL(event.data)
    }
  }

  mediaRecorder.start(250) // send chunks every 250ms

  // 4. After vendor speaks, stop and verify
  setTimeout(async () => {
    mediaRecorder.stop()
    socket.close()

    // 5. Send transcript to backend for verification
    const result = await fetch('/api/v1/vendor/voice/verify', {
      method: 'POST',
      body: JSON.stringify({
        session_id: sessionId,
        transcript: finalTranscript,
        audio_confidence: audioConfidence,
        multiple_speakers_detected: false
      })
    }).then(r => r.json())

    handleVoiceResult(result)
  }, 8000) // 8 seconds to speak the phrase
}
```

---

## Project Structure

```
vault-api/
├── main.py                          # FastAPI app entry point
├── requirements.txt
├── .env.example
└── app/
    ├── core/
    │   └── config.py                # Settings + env vars
    ├── models/
    │   └── schemas.py               # Pydantic request/response models
    ├── routers/
    │   └── vault.py                 # All API endpoints
    └── services/
        ├── liveness_service.py      # MediaPipe server-side validation
        ├── voice_service.py         # AssemblyAI + phrase matching
        ├── identity_service.py      # Youverify NIN + face match
        ├── vault_score_service.py   # VaultScore calculation
        └── escrow_service.py        # Squad escrow + GPS verification
```
