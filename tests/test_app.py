import asyncio
import json
import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("DEEPGRAM_API_KEY", "test-key")

import app
from deepgram.core.api_error import ApiError


class FakeWebSocket:
    def __init__(self, token, messages, query_params=None, receive_delay=0):
        self.headers = {"sec-websocket-protocol": f"access_token.{token}"}
        self.query_params = query_params or {}
        self.messages = iter(messages)
        self.receive_delay = receive_delay
        self.text_messages = []
        self.byte_messages = []

    async def accept(self, subprotocol):
        self.subprotocol = subprotocol

    async def close(self, code, reason):
        self.closed = (code, reason)

    async def receive(self):
        if self.receive_delay:
            await asyncio.sleep(self.receive_delay)
        return next(self.messages)

    async def send_text(self, message):
        self.text_messages.append(message)

    async def send_bytes(self, message):
        self.byte_messages.append(message)


class FakeConnection:
    def __init__(self, messages=(), stream_error=None):
        self.messages = messages
        self.stream_error = stream_error
        self.media = []
        self.controls = []

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        for message in self.messages:
            yield message
        if self.stream_error:
            raise self.stream_error

    async def send_media(self, audio):
        self.media.append(audio)

    async def send_keep_alive(self):
        self.controls.append("KeepAlive")

    async def send_finalize(self):
        self.controls.append("Finalize")

    async def send_close_stream(self):
        self.controls.append("CloseStream")


class FakeConnect:
    def __init__(self, connection=None, connect_error=None):
        self.connection = connection
        self.connect_error = connect_error

    async def __aenter__(self):
        if self.connect_error:
            raise self.connect_error
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeListenV1:
    def __init__(self, connect):
        self.connect_result = connect
        self.query = None

    def connect(self, **query):
        self.query = query
        return self.connect_result


class FakeDeepgram:
    def __init__(self, connect):
        self.listen = type("Listen", (), {"v1": FakeListenV1(connect)})()


class ModelMessage:
    def model_dump_json(self):
        return '{"type":"Results","is_final":true}'


class LiveTranscriptionTests(unittest.IsolatedAsyncioTestCase):
    def token(self):
        return app.jwt.encode(
            {"iat": int(time.time()), "exp": int(time.time()) + 60},
            app.SESSION_SECRET,
            algorithm="HS256",
        )

    async def test_routes_media_controls_and_results_through_sdk(self):
        connection = FakeConnection(messages=[ModelMessage()])
        sdk = FakeDeepgram(FakeConnect(connection=connection))
        websocket = FakeWebSocket(
            self.token(),
            [
                {"bytes": b"audio"},
                {"text": '{"type":"KeepAlive"}'},
                {"text": '{"type":"Finalize"}'},
                {"text": '{"type":"CloseStream"}'},
                {"type": "websocket.disconnect"},
            ],
            query_params={"model": "nova-3", "language": "es"},
            receive_delay=0.001,
        )

        with patch.object(app, "deepgram", sdk):
            await app.live_transcription(websocket)

        self.assertEqual(connection.media, [b"audio"])
        self.assertEqual(connection.controls, ["KeepAlive", "Finalize", "CloseStream"])
        self.assertEqual(sdk.listen.v1.query["model"], "nova-3")
        self.assertEqual(sdk.listen.v1.query["language"], "es")
        self.assertEqual(
            websocket.text_messages,
            ['{"type":"Results","is_final":true}'],
        )

    async def test_connect_error_never_exposes_authorization_header(self):
        secret = "test-connect-secret"
        error = ApiError(
            status_code=400,
            headers={"Authorization": f"Token {secret}"},
            body="invalid request",
        )
        websocket = FakeWebSocket(self.token(), [])

        with patch.object(app, "deepgram", FakeDeepgram(FakeConnect(connect_error=error))):
            with patch("builtins.print") as log:
                await app.live_transcription(websocket)

        observed = "\n".join(websocket.text_messages)
        observed += "\n".join(
            str(arg) for call in log.call_args_list for arg in call.args
        )
        self.assertIn("Deepgram rejected the connection (HTTP 400)", observed)
        self.assertNotIn(secret, observed)
        self.assertNotIn("Authorization", observed)

    async def test_stream_error_never_exposes_authorization_header(self):
        secret = "test-stream-secret"
        error = ApiError(
            status_code=401,
            headers={"Authorization": f"Token {secret}"},
            body="invalid credentials",
        )
        sdk = FakeDeepgram(
            FakeConnect(connection=FakeConnection(stream_error=error))
        )
        websocket = FakeWebSocket(
            self.token(),
            [{"type": "websocket.disconnect"}],
            receive_delay=0.01,
        )

        with patch.object(app, "deepgram", sdk):
            with patch("builtins.print") as log:
                await app.live_transcription(websocket)

        observed = "\n".join(websocket.text_messages)
        observed += "\n".join(
            str(arg) for call in log.call_args_list for arg in call.args
        )
        self.assertEqual(
            json.loads(websocket.text_messages[0])["code"], "PROVIDER_ERROR"
        )
        self.assertIn("Deepgram stream error (HTTP 401)", observed)
        self.assertNotIn(secret, observed)
        self.assertNotIn("Authorization", observed)


if __name__ == "__main__":
    unittest.main()
