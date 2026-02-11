"""
FastAPI Live Transcription Starter - Raw WebSocket proxy to Deepgram

Key Features:
- WebSocket endpoint: /api/live-transcription
- JWT session auth for API protection
- Raw WebSocket proxy to Deepgram STT API
"""

import os
import json
import secrets
import time
import asyncio

import jwt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Depends
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import websockets
import toml

load_dotenv(override=False)

CONFIG = {
    "port": int(os.environ.get("PORT", 8081)),
    "host": os.environ.get("HOST", "0.0.0.0"),
}

def load_api_key():
    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        raise ValueError("DEEPGRAM_API_KEY required")
    return api_key

API_KEY = load_api_key()
DEEPGRAM_STT_URL = "wss://api.deepgram.com/v1/listen"

# ============================================================================
# SESSION AUTH - JWT tokens for API protection
# ============================================================================

SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
JWT_EXPIRY = 3600  # 1 hour


# Read frontend/dist/index.html for serving
_index_html_template = None
try:
    with open(os.path.join(os.path.dirname(__file__), "frontend", "dist", "index.html")) as f:
        _index_html_template = f.read()
except FileNotFoundError:
    pass  # No built frontend (dev mode)


def require_session(authorization: str = Header(None)):
    """FastAPI dependency for JWT session validation."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "type": "AuthenticationError",
                    "code": "MISSING_TOKEN",
                    "message": "Authorization header with Bearer token is required",
                }
            }
        )
    token = authorization[7:]
    try:
        jwt.decode(token, SESSION_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "type": "AuthenticationError",
                    "code": "INVALID_TOKEN",
                    "message": "Session expired, please refresh the page",
                }
            }
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "type": "AuthenticationError",
                    "code": "INVALID_TOKEN",
                    "message": "Invalid session token",
                }
            }
        )


app = FastAPI(title="Deepgram Live STT API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# SESSION ROUTES - Auth endpoints (unprotected)
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve index.html."""
    if not _index_html_template:
        raise HTTPException(status_code=404, detail="Frontend not built. Run make build first.")
    return HTMLResponse(content=_index_html_template)


@app.get("/api/session")
async def get_session():
    """Issues a JWT session token."""
    token = jwt.encode(
        {"iat": int(time.time()), "exp": int(time.time()) + JWT_EXPIRY},
        SESSION_SECRET,
        algorithm="HS256",
    )
    return JSONResponse(content={"token": token})


# ============================================================================
# WEBSOCKET ROUTE
# ============================================================================

@app.websocket("/api/live-transcription")
async def live_transcription(websocket: WebSocket):
    """Raw WebSocket proxy endpoint for live STT"""
    # Validate JWT from subprotocol
    protocols = websocket.headers.get("sec-websocket-protocol", "")
    protocol_list = [p.strip() for p in protocols.split(",")]
    valid_proto = None
    for proto in protocol_list:
        if proto.startswith("access_token."):
            token = proto[len("access_token."):]
            try:
                jwt.decode(token, SESSION_SECRET, algorithms=["HS256"])
                valid_proto = proto
            except Exception:
                pass
            break

    if not valid_proto:
        await websocket.close(code=4401, reason="Unauthorized")
        return

    await websocket.accept(subprotocol=valid_proto)
    print("Client connected to /api/live-transcription")

    deepgram_ws = None
    forward_task = None
    stop_event = asyncio.Event()

    try:
        # Get query parameters
        model = websocket.query_params.get("model", "nova-2")
        language = websocket.query_params.get("language", "en")
        smart_format = websocket.query_params.get("smart_format", "true")
        interim_results = websocket.query_params.get("interim_results", "true")
        punctuate = websocket.query_params.get("punctuate", "true")
        encoding = websocket.query_params.get("encoding", "linear16")
        sample_rate = websocket.query_params.get("sample_rate", "16000")

        # Build Deepgram WebSocket URL with parameters
        deepgram_url = (
            f"{DEEPGRAM_STT_URL}?"
            f"model={model}&"
            f"language={language}&"
            f"smart_format={smart_format}&"
            f"interim_results={interim_results}&"
            f"punctuate={punctuate}&"
            f"encoding={encoding}&"
            f"sample_rate={sample_rate}"
        )

        print(f"Connecting to Deepgram STT: model={model}, language={language}")

        # Connect to Deepgram
        deepgram_ws = await websockets.connect(
            deepgram_url,
            additional_headers={"Authorization": f"Token {API_KEY}"}
        )
        print("✓ Connected to Deepgram STT API")

        # Task to forward messages from Deepgram to client
        async def forward_from_deepgram():
            try:
                async for message in deepgram_ws:
                    if stop_event.is_set():
                        break

                    # Forward message to client
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            except websockets.exceptions.ConnectionClosed as e:
                print(f"Deepgram connection closed: {e.code} {e.reason}")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"Error forwarding from Deepgram: {e}")
                await websocket.send_text(json.dumps({
                    "type": "Error",
                    "description": str(e),
                    "code": "PROVIDER_ERROR"
                }))

        # Start forwarding task
        forward_task = asyncio.create_task(forward_from_deepgram())

        # Forward messages from client to Deepgram
        try:
            while True:
                message = await websocket.receive()

                if "text" in message:
                    await deepgram_ws.send(message["text"])
                elif "bytes" in message:
                    await deepgram_ws.send(message["bytes"])

        except WebSocketDisconnect:
            print("Client disconnected")
        except Exception as e:
            print(f"Error forwarding to Deepgram: {e}")

    except Exception as e:
        print(f"WebSocket error: {e}")
        await websocket.send_text(json.dumps({
            "type": "Error",
            "description": str(e),
            "code": "CONNECTION_FAILED"
        }))

    finally:
        # Cleanup
        stop_event.set()

        if forward_task:
            forward_task.cancel()
            try:
                await forward_task
            except asyncio.CancelledError:
                pass

        if deepgram_ws:
            try:
                await deepgram_ws.close()
            except Exception as e:
                print(f"Error closing Deepgram connection: {e}")

        print("Connection cleanup complete")

@app.get("/api/metadata")
async def get_metadata():
    try:
        with open('deepgram.toml', 'r') as f:
            config = toml.load(f)
        return JSONResponse(content=config.get('meta', {}))
    except:
        return JSONResponse(status_code=500, content={"error": "Metadata read failed"})

if __name__ == "__main__":
    import uvicorn
    print(f"\n🚀 FastAPI Live STT Server: http://localhost:{CONFIG['port']}")
    print(f"   GET  /api/session")
    print(f"   WS   /api/live-transcription (auth required)")
    print(f"   GET  /api/metadata\n")
    uvicorn.run(app, host=CONFIG["host"], port=CONFIG["port"])
