"""
FastAPI Live Transcription Starter - Deepgram SDK WebSocket proxy

Key Features:
- WebSocket endpoint: /api/live-transcription
- JWT session auth for API protection
- Deepgram SDK proxy to the Live STT API
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
import toml

from deepgram import AsyncDeepgramClient
from deepgram.environment import DeepgramClientEnvironment
from deepgram.core.api_error import ApiError
from deepgram.listen.v1.types import ListenV1Finalize
from websockets.exceptions import ConnectionClosedOK

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


# One async SDK client, reused across connections; the browser never sees the API key.
# DEEPGRAM_BASE_URL (e.g. wss://api.staging.deepgram.com) overrides the default
# production endpoint used for the /v1/listen websocket.
def _build_client():
    base_url = os.environ.get("DEEPGRAM_BASE_URL")
    if base_url:
        https = base_url.replace("wss://", "https://").replace("ws://", "http://")
        env = DeepgramClientEnvironment(
            base=https, production=base_url, agent=base_url, agent_rest=https
        )
        print(f"Using custom Deepgram base URL: {base_url}")
        return AsyncDeepgramClient(api_key=API_KEY, environment=env)
    return AsyncDeepgramClient(api_key=API_KEY)


deepgram = _build_client()

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
    """SDK-backed WebSocket proxy endpoint for live STT"""
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

    # Get query parameters
    model = websocket.query_params.get("model", "nova-2")
    language = websocket.query_params.get("language", "en")
    smart_format = websocket.query_params.get("smart_format", "true")
    interim_results = websocket.query_params.get("interim_results", "true")
    punctuate = websocket.query_params.get("punctuate", "true")
    encoding = websocket.query_params.get("encoding", "linear16")
    sample_rate = websocket.query_params.get("sample_rate", "16000")
    channels = websocket.query_params.get("channels", "1")
    typed_parameters = {
        "model", "language", "smart_format", "interim_results", "punctuate",
        "encoding", "sample_rate", "channels",
    }
    extra_query_parameters = {
        name: value for name, value in websocket.query_params.items()
        if name not in typed_parameters
    }

    print(f"Connecting to Deepgram STT: model={model}, language={language}")

    try:
        # Connect to Deepgram live STT through the official SDK
        async with deepgram.listen.v1.connect(
            model=model,
            language=language,
            smart_format=smart_format,
            interim_results=interim_results,
            punctuate=punctuate,
            encoding=encoding,
            sample_rate=sample_rate,
            channels=channels,
            request_options=(
                {"additional_query_parameters": extra_query_parameters}
                if extra_query_parameters else None
            ),
        ) as connection:
            print("✓ Connected to Deepgram STT API")

            # Task to forward transcription results from Deepgram to the client
            async def forward_from_deepgram():
                try:
                    while True:
                        # recv() preserves unsupported frames as dictionaries;
                        # the async iterator logs and silently skips them.
                        message = await connection.recv()
                        if isinstance(message, (bytes, bytearray)):
                            await websocket.send_bytes(bytes(message))
                        elif isinstance(message, dict) and message.get("type") == "Error":
                            await websocket.send_text(json.dumps({
                                "type": "Error",
                                "description": "Deepgram reported a stream error",
                                "code": "PROVIDER_ERROR"
                            }))
                        elif isinstance(message, dict):
                            await websocket.send_text(json.dumps(message))
                        elif hasattr(message, "model_dump_json"):
                            await websocket.send_text(message.model_dump_json())
                        else:
                            await websocket.send_text(
                                json.dumps({"type": getattr(message, "type", "Unknown")})
                            )
                except (asyncio.CancelledError, ConnectionClosedOK):
                    pass
                except Exception as e:
                    # Sanitized mid-stream error. Never surface str(e): an
                    # ApiError's string form embeds the Authorization: Token
                    # <key> header (deepgram-sdk 7.6.0).
                    detail = (f"Deepgram stream error (HTTP {e.status_code})"
                              if isinstance(e, ApiError) else
                              f"Deepgram stream error ({type(e).__name__})")
                    print(f"Error forwarding from Deepgram: {detail}")
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "Error",
                            "description": detail,
                            "code": "PROVIDER_ERROR"
                        }))
                    except Exception:
                        pass

            # Start forwarding task
            forward_task = asyncio.create_task(forward_from_deepgram())

            # Forward audio + control messages from client to Deepgram
            try:
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        break

                    audio = message.get("bytes")
                    if audio is not None:
                        await connection.send_media(audio)
                        continue

                    text = message.get("text")
                    if text is None:
                        continue

                    # Browser control messages (KeepAlive / Finalize / CloseStream)
                    try:
                        control = json.loads(text)
                    except (ValueError, TypeError):
                        print("Ignoring non-JSON message from client")
                        continue

                    ctype = control.get("type")
                    if ctype == "KeepAlive":
                        await connection.send_keep_alive()
                    elif ctype == "Finalize":
                        if "channel" in control:
                            await connection.send_finalize(
                                ListenV1Finalize(type="Finalize", channel=control["channel"])
                            )
                        else:
                            await connection.send_finalize()
                    elif ctype == "CloseStream":
                        await connection.send_close_stream()
                    else:
                        print(f"Ignoring unknown client message type: {ctype}")

            except WebSocketDisconnect:
                print("Client disconnected")
            finally:
                forward_task.cancel()
                try:
                    await forward_task
                except asyncio.CancelledError:
                    pass

    except Exception as e:
        # Sanitized connect error. Never surface str(e): an ApiError's string
        # form embeds the Authorization: Token <key> header (deepgram-sdk 7.6.0),
        # which would otherwise reach both the server log and the browser.
        detail = (f"Deepgram rejected the connection (HTTP {e.status_code})"
                  if isinstance(e, ApiError) else
                  f"Failed to connect to Deepgram ({type(e).__name__})")
        print(f"WebSocket error: {detail}")
        try:
            await websocket.send_text(json.dumps({
                "type": "Error",
                "description": detail,
                "code": "CONNECTION_FAILED"
            }))
        except Exception:
            pass

    finally:
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
