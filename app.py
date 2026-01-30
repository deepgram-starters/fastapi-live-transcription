"""
FastAPI Live Transcription Starter

Async WebSocket proxy to Deepgram's Live STT API.
"""

import os
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
from dotenv import load_dotenv
import toml

load_dotenv(override=False)

def load_api_key():
    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        raise ValueError("DEEPGRAM_API_KEY required")
    return api_key

api_key = load_api_key()
deepgram = DeepgramClient(api_key=api_key)

app = FastAPI(title="Deepgram Live STT API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.websocket("/stt/stream")
async def live_transcription(websocket: WebSocket):
    """WebSocket endpoint for live STT"""
    await websocket.accept()
    print("Client connected to /stt/stream")

    try:
        # Get query parameters
        model = websocket.query_params.get("model", "nova-2")
        language = websocket.query_params.get("language", "en")

        # Create Deepgram connection
        dg_connection = deepgram.listen.asyncwebsocket.v("1")
        
        async def on_message(self, result, **kwargs):
            await websocket.send_text(result)

        async def on_error(self, error, **kwargs):
            print(f"Deepgram error: {error}")

        dg_connection.on(LiveTranscriptionEvents.Transcript, on_message)
        dg_connection.on(LiveTranscriptionEvents.Error, on_error)

        # Start Deepgram connection
        options = LiveOptions(
            model=model,
            language=language,
            smart_format=True,
            encoding="linear16",
            sample_rate=16000,
            channels=1
        )
        
        await dg_connection.start(options)

        # Forward messages bidirectionally
        async def receive_from_client():
            try:
                while True:
                    message = await websocket.receive_bytes()
                    await dg_connection.send(message)
            except WebSocketDisconnect:
                pass

        await receive_from_client()

    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        try:
            await dg_connection.finish()
        except:
            pass
        print("Client disconnected")

@app.get("/api/metadata")
async def get_metadata():
    try:
        with open('deepgram.toml', 'r') as f:
            config = toml.load(f)
        return JSONResponse(content=config.get('meta', {}))
    except:
        return JSONResponse(status_code=500, content={"error": "Metadata read failed"})

app.mount("/", StaticFiles(directory="frontend/dist", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    print(f"\n🚀 FastAPI Live STT Server: http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port)
