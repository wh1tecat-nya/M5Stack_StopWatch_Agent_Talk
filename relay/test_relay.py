import base64
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import tempfile
import threading
import unittest
from pathlib import Path

from .server import EVENT, Handler, Relay, REPLY_TOOL


class AudioFixture:
    def __init__(self):
        self.transcriptions = []
        self.speeches = []

    def transcribe(self, pcm):
        self.transcriptions.append(pcm)
        return "こんにちは"

    def speak(self, text):
        self.speeches.append(text)
        return b"\x01\x02" * 480


class CallbackFixture:
    def __init__(self):
        self.events = []
        self.status = 202

    def post(self, url, keys, subscription_id, event_id, data):
        if data.get("type") == "verification":
            return 200, json.dumps({"challenge": data["challenge"]}).encode()
        self.events.append(data)
        return self.status, b""


class RelayTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "test.sqlite3"
        self.audio = AudioFixture()
        self.callback = CallbackFixture()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.relay = self.restart_relay()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def restart_relay(self):
        self.relay = Relay(self.database, "device-token", "mcp-token", self.audio, self.callback)
        self.server.relay = self.relay
        return self.relay

    def request(self, method, path, body=None, mcp=False, token=None):
        connection = HTTPConnection(*self.server.server_address, timeout=3)
        headers = {
            "Authorization": "Bearer " + (token or ("mcp-token" if mcp else "device-token")),
            "Content-Type": "application/json" if mcp else "application/octet-stream",
        }
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, response.getheader("X-Reply-Id"), response.read()
        finally:
            connection.close()

    def rpc(self, method, params=None):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        status, _, response = self.request("POST", "/mcp", body, mcp=True)
        self.assertEqual(status, 200)
        return json.loads(response)

    def subscribe(self):
        subscription = self.rpc("events/subscribe", {
            "name": EVENT, "arguments": {"device_id": "stopwatch"},
            "delivery": {
                "mode": "webhook", "url": "https://example.com/callback",
                "secret": "whsec_" + base64.b64encode(b"1" * 32).decode(),
            },
        })
        self.assertIn("result", subscription)

    def test_voice_round_trip_survives_restart_and_duplicate_requests(self):
        request_id = "1" * 32
        upload = "/device/turns/" + request_id
        pcm = b"\x04\x03" * 3200
        self.assertEqual(self.request("PUT", upload, pcm, token="wrong")[0], 401)
        self.assertEqual(self.request("PUT", upload, pcm)[0], 409)
        self.assertIn("events", self.rpc("server/discover")["result"]["capabilities"])
        self.subscribe()
        self.assertEqual(self.request("PUT", upload, pcm)[0], 202)
        self.assertEqual(self.request("PUT", upload, pcm)[0], 202)
        self.assertEqual(self.request("PUT", upload, b"\x00" * len(pcm))[0], 409)
        self.restart_relay()
        self.assertTrue(self.relay.advance())
        self.assertTrue(self.relay.advance())
        self.assertEqual(self.audio.transcriptions, [pcm])
        self.assertEqual([event["data"] for event in self.callback.events], [
            {"request_id": request_id, "device_id": "stopwatch", "text": "こんにちは"},
        ])
        self.assertEqual(self.request("GET", "/device/replies/next")[0], 204)
        reply = {"name": REPLY_TOOL["name"], "arguments": {"request_id": request_id, "text": "届いたよ"}}
        self.assertIn("result", self.rpc("tools/call", reply))
        self.assertIn("result", self.rpc("tools/call", reply))
        self.assertTrue(self.relay.advance())
        self.assertEqual(self.audio.speeches, ["届いたよ"])
        expected = (200, request_id, b"\x01\x02" * 480)
        self.assertEqual(self.request("GET", "/device/replies/next"), expected)
        self.assertEqual(self.request("GET", "/device/replies/next"), expected)
        for _ in range(2):
            self.assertEqual(self.request("DELETE", "/device/replies/" + request_id)[0], 204)
        self.assertEqual(self.request("GET", "/device/replies/next")[0], 204)
        self.assertIn("result", self.rpc("tools/call", reply))
        self.assertFalse(self.relay.advance())

    def test_removed_callback_is_terminal_and_acknowledgeable(self):
        self.subscribe()
        request_id = "2" * 32
        self.request("PUT", "/device/turns/" + request_id, b"\x00" * 6400)
        self.relay.advance()
        self.callback.status = 410
        with self.assertLogs(level="WARNING"):
            self.relay.advance()
        self.assertEqual(self.request("GET", "/device/replies/next"), (422, request_id, b""))
        self.assertEqual(self.request("DELETE", "/device/replies/" + request_id)[0], 204)
        self.assertEqual(self.request("GET", "/device/replies/next")[0], 204)


if __name__ == "__main__":
    unittest.main()
