import argparse
import base64
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from .audio import Audio, MAX_INPUT, MAX_OUTPUT, encode_json
from .callback import Callback, CallbackError

EVENT = "stopwatch.message"
DEVICE = "stopwatch"
TURN_ID = re.compile(r"[a-f0-9]{32}")
TURN_TTL = 600
PROTOCOL = "2026-07-28"
STREAMABLE_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")


def iso_time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def object_schema(**properties):
    return {
        "type": "object", "properties": properties,
        "required": list(properties), "additionalProperties": False,
    }


EVENT_DEFINITION = {
    "name": EVENT,
    "description": "A spoken message recorded by the owner on their StopWatch.",
    "delivery": ["webhook"],
    "inputSchema": object_schema(device_id={"type": "string", "enum": [DEVICE]}),
    "payloadSchema": object_schema(
        device_id={"type": "string"}, request_id={"type": "string"}, text={"type": "string"},
    ),
}
REPLY_TOOL = {
    "name": "submit_stopwatch_reply",
    "description": "Send the reply for the given stopwatch.message request to the owner's StopWatch.",
    "inputSchema": object_schema(
        request_id={"type": "string", "pattern": "^[a-f0-9]{32}$"},
        text={"type": "string", "minLength": 1, "maxLength": 300},
    ),
}


class RpcError(Exception):
    def __init__(self, code, message, data=None):
        self.error = {"code": code, "message": message}
        if data is not None:
            self.error["data"] = data


class Relay:
    def __init__(self, database, device_token, mcp_token, audio, callback):
        self.database = database
        self.device_token = device_token
        self.mcp_token = mcp_token
        self.audio = audio
        self.callback = callback
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS subscription (
                    slot INTEGER PRIMARY KEY CHECK (slot = 1),
                    id TEXT NOT NULL, url TEXT NOT NULL, secret BLOB NOT NULL,
                    expires REAL NOT NULL, verified REAL NOT NULL,
                    old_secret BLOB, rotate_until REAL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, digest BLOB NOT NULL,
                    subscription_id TEXT NOT NULL, state TEXT NOT NULL,
                    pcm BLOB, transcript TEXT, answer TEXT, speech BLOB,
                    attempts INTEGER NOT NULL DEFAULT 0, retry_after REAL NOT NULL DEFAULT 0
                );
            """)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def subscription(self, db):
        return db.execute("SELECT * FROM subscription WHERE expires > ?", (time.time(),)).fetchone()

    def upload(self, request_id, pcm):
        digest = hashlib.sha256(pcm).digest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT digest FROM turns WHERE id = ?", (request_id,)).fetchone()
            if prior:
                return 202 if prior["digest"] == digest else 409
            subscription = self.subscription(db)
            if not subscription:
                return 409
            count = db.execute("SELECT count(*) FROM turns WHERE state != 'played'").fetchone()[0]
            if count >= 8:
                return 429
            db.execute(
                """INSERT INTO turns(id, created, digest, subscription_id, state, pcm)
                   VALUES (?, ?, ?, ?, 'audio', ?)""",
                (request_id, time.time(), digest, subscription["id"], pcm),
            )
            return 202

    def next_reply(self):
        with self.connect() as db:
            return db.execute(
                "SELECT id, state, speech FROM turns WHERE state IN ('ready', 'failed') ORDER BY created LIMIT 1"
            ).fetchone()

    def acknowledge(self, request_id):
        with self.connect() as db:
            db.execute(
                "UPDATE turns SET state = 'played', speech = NULL WHERE id = ? AND state IN ('ready', 'failed')",
                (request_id,),
            )

    def subscription_rpc(self, params, remove):
        delivery = params["delivery"]
        if (params["name"] != EVENT or params["arguments"] != {"device_id": DEVICE}
                or delivery["mode"] != "webhook" or params.get("cursor") is not None):
            raise RpcError(-32602, "Invalid subscription")
        url = delivery["url"]
        subscription_id = "sub_" + hashlib.sha256(url.encode()).hexdigest()[:32]
        with self.connect() as db:
            if remove:
                db.execute("DELETE FROM subscription WHERE id = ?", (subscription_id,))
                return {}
            existing = self.subscription(db)
        if existing and existing["id"] != subscription_id:
            raise RpcError(-32602, "An active StopWatch subscription already exists")
        secret = delivery["secret"]
        if not isinstance(secret, str) or not secret.startswith("whsec_") or len(secret) > 100:
            raise RpcError(-32602, "Invalid signing secret")
        key = base64.b64decode(secret[6:], validate=True)
        if not 24 <= len(key) <= 64:
            raise RpcError(-32602, "Invalid signing secret")
        ttl = params.get("ttlMs")
        if ttl is not None and (type(ttl) is not int or ttl <= 0):
            raise RpcError(-32602, "Invalid ttlMs")
        now = time.time()
        verified = existing["verified"] if existing and existing["secret"] == key else 0
        if now - verified > 300:
            challenge = secrets.token_urlsafe(32)
            try:
                status, body = self.callback.post(
                    url, [key], subscription_id, "verify_" + secrets.token_hex(16),
                    {"type": "verification", "challenge": challenge},
                )
                if not 200 <= status < 300 or not hmac.compare_digest(
                    json.loads(body)["challenge"].encode(), challenge.encode()
                ):
                    raise CallbackError("challenge_failed")
            except (CallbackError, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                reason = str(error) if isinstance(error, CallbackError) else "challenge_failed"
                raise RpcError(-32015, "Callback verification failed", {"reason": reason}) from error
            verified = time.time()
        expires = time.time() + (min(ttl / 1000, 86400) if ttl else 86400)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = self.subscription(db)
            if latest and latest["id"] != subscription_id:
                raise RpcError(-32602, "An active StopWatch subscription already exists")
            old_key = latest["old_secret"] if latest else None
            rotate_until = latest["rotate_until"] if latest else None
            if latest and latest["secret"] != key:
                old_key, rotate_until = latest["secret"], time.time() + 300
            db.execute(
                "REPLACE INTO subscription VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                (subscription_id, url, key, expires, verified, old_key, rotate_until),
            )
        return {"id": subscription_id, "refreshBefore": iso_time(expires), "cursor": None, "truncated": False}

    def submit_reply(self, arguments):
        request_id = arguments["request_id"]
        answer = arguments["text"].strip()
        if not TURN_ID.fullmatch(request_id) or not 1 <= len(answer) <= 300:
            raise RpcError(-32602, "Invalid reply")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            turn = db.execute("SELECT * FROM turns WHERE id = ?", (request_id,)).fetchone()
            sub = self.subscription(db)
            if not turn or not sub or turn["subscription_id"] != sub["id"]:
                raise RpcError(-32602, "No pending request")
            if turn["state"] in ("speech", "ready", "played") and turn["answer"] == answer:
                return
            if turn["state"] not in ("notify", "waiting") or turn["created"] < time.time() - TURN_TTL:
                raise RpcError(-32602, "Request expired or already answered")
            db.execute(
                """UPDATE turns SET state = 'speech', answer = ?, transcript = NULL,
                   attempts = 0, retry_after = 0 WHERE id = ?""",
                (answer, request_id),
            )

    def rpc(self, method, params):
        if method == "server/discover":
            return {"resultType": "complete", "supportedVersions": [PROTOCOL],
                    "serverInfo": {"name": "sumi-stopwatch", "version": "0.1.0"},
                    "capabilities": {"tools": {}, "events": {}}}
        if method == "initialize":
            version = params["protocolVersion"]
            return {"protocolVersion": version if version in STREAMABLE_VERSIONS else STREAMABLE_VERSIONS[-1],
                    "serverInfo": {"name": "sumi-stopwatch", "version": "0.1.0"},
                    "capabilities": {"tools": {}, "events": {}}}
        if method == "ping":
            return {}
        if method == "events/list":
            return {"events": [EVENT_DEFINITION]}
        if method in ("events/subscribe", "events/unsubscribe"):
            return self.subscription_rpc(params, method == "events/unsubscribe")
        if method == "tools/list":
            return {"tools": [REPLY_TOOL]}
        if method == "tools/call" and params["name"] == REPLY_TOOL["name"]:
            self.submit_reply(params["arguments"])
            return {"content": [{"type": "text", "text": "Reply queued for playback on StopWatch."}]}
        raise RpcError(-32601, "Unknown method")

    def advance(self):
        now = time.time()
        with self.connect() as db:
            db.execute("DELETE FROM turns WHERE created < ?", (now - 86400,))
            db.execute("DELETE FROM subscription WHERE expires <= ?", (now,))
            db.execute("UPDATE subscription SET old_secret = NULL WHERE rotate_until <= ?", (now,))
            db.execute(
                """UPDATE turns SET state = 'failed', pcm = NULL, transcript = NULL, answer = NULL
                   WHERE created < ? AND state IN ('audio', 'notify', 'waiting', 'speech')""",
                (now - TURN_TTL,),
            )
            turn = db.execute(
                """SELECT * FROM turns WHERE state IN ('audio', 'notify', 'speech')
                   AND retry_after <= ? ORDER BY created LIMIT 1""", (now,),
            ).fetchone()
            subscription = self.subscription(db)
        if turn is None:
            return False
        state = turn["state"]
        try:
            if state == "audio":
                transcript = self.audio.transcribe(turn["pcm"])
                update = ("state = 'notify', transcript = ?, pcm = NULL", (transcript,))
            elif state == "notify":
                if not subscription or subscription["id"] != turn["subscription_id"]:
                    raise CallbackError("subscription_unavailable")
                keys = [subscription["secret"]]
                if subscription["old_secret"] and subscription["rotate_until"] > now:
                    keys.append(subscription["old_secret"])
                status, _ = self.callback.post(
                    subscription["url"], keys, subscription["id"], "evt_" + turn["id"], {
                        "eventId": "evt_" + turn["id"], "name": EVENT, "timestamp": iso_time(turn["created"]),
                        "data": {"device_id": DEVICE, "request_id": turn["id"], "text": turn["transcript"]},
                        "cursor": None,
                    },
                )
                if status in (410, 413):
                    raise CallbackError(f"callback_{status}")
                if not 200 <= status < 300:
                    raise RuntimeError(f"Callback returned HTTP {status}")
                update = ("state = 'waiting', transcript = NULL", ())
            else:
                audio = self.audio.speak(turn["answer"])
                if not 0 < len(audio) <= MAX_OUTPUT or len(audio) % 2:
                    raise RuntimeError("Invalid reply PCM length")
                update = ("state = 'ready', speech = ?", (audio,))
            with self.connect() as db:
                db.execute(
                    f"UPDATE turns SET {update[0]}, attempts = 0, retry_after = 0 WHERE id = ? AND state = ?",
                    (*update[1], turn["id"], state),
                )
        except Exception as error:
            logging.warning("%s failed (%s)", state, type(error).__name__)
            attempts = turn["attempts"] + 1
            with self.connect() as db:
                if attempts >= 3 or isinstance(error, CallbackError):
                    db.execute(
                        """UPDATE turns SET state = 'failed', pcm = NULL, transcript = NULL, answer = NULL
                           WHERE id = ? AND state = ?""", (turn["id"], state),
                    )
                else:
                    db.execute(
                        "UPDATE turns SET attempts = ?, retry_after = ? WHERE id = ? AND state = ?",
                        (attempts, time.time() + 2 ** attempts, turn["id"], state),
                    )
        return True

    def worker(self, stop):
        while not stop.is_set():
            try:
                if self.advance():
                    continue
            except sqlite3.Error:
                logging.exception("Relay database unavailable")
            stop.wait(1)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, format, *args):
        pass

    def respond(self, status, body=b"", content_type=None, reply_id=None):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if content_type:
            self.send_header("Content-Type", content_type)
        if reply_id:
            self.send_header("X-Reply-Id", reply_id)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def authorized(self, token):
        expected = ("Bearer " + token).encode()
        received = self.headers.get("Authorization", "").encode()
        if hmac.compare_digest(received, expected):
            return True
        self.respond(401)
        return False

    def read_body(self, maximum, content_type):
        if self.headers.get_content_type() != content_type or self.headers.get("Transfer-Encoding"):
            raise ValueError("Unsupported body")
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= maximum:
            raise ValueError("Invalid body size")
        body = self.rfile.read(size)
        if len(body) != size:
            raise ValueError("Incomplete body")
        return body

    def do_PUT(self):
        prefix = "/device/turns/"
        request_id = self.path.removeprefix(prefix)
        if not self.path.startswith(prefix) or not TURN_ID.fullmatch(request_id):
            return self.respond(404)
        if not self.authorized(self.server.relay.device_token):
            return
        try:
            body = self.read_body(MAX_INPUT, "application/octet-stream")
            if len(body) < 6400 or len(body) % 2:
                raise ValueError("Invalid PCM")
        except (ValueError, TimeoutError):
            return self.respond(400)
        self.respond(self.server.relay.upload(request_id, body))

    def do_GET(self):
        if self.path == "/mcp":
            return self.respond(405)
        if self.path != "/device/replies/next":
            return self.respond(404)
        if not self.authorized(self.server.relay.device_token):
            return
        reply = self.server.relay.next_reply()
        if not reply:
            return self.respond(204)
        if reply["state"] == "failed":
            return self.respond(422, reply_id=reply["id"])
        self.respond(200, reply["speech"], "application/octet-stream", reply["id"])

    def do_DELETE(self):
        prefix = "/device/replies/"
        request_id = self.path.removeprefix(prefix)
        if not self.path.startswith(prefix) or not TURN_ID.fullmatch(request_id):
            return self.respond(404)
        if self.authorized(self.server.relay.device_token):
            self.server.relay.acknowledge(request_id)
            self.respond(204)

    def do_POST(self):
        if self.path != "/mcp":
            return self.respond(404)
        if not self.authorized(self.server.relay.mcp_token):
            return
        request_id = None
        try:
            request = json.loads(self.read_body(16384, "application/json"))
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise RpcError(-32600, "Invalid request")
            request_id = request.get("id")
            method = request["method"]
            if request_id is None and method in ("notifications/initialized", "notifications/cancelled"):
                return self.respond(202)
            result = {"result": self.server.relay.rpc(method, request.get("params", {}))}
        except RpcError as error:
            result = {"error": error.error}
        except (ValueError, TypeError, KeyError, AttributeError, TimeoutError):
            result = {"error": {"code": -32602, "message": "Invalid parameters"}}
        self.respond(200, encode_json({"jsonrpc": "2.0", "id": request_id, **result}), "application/json")


def main():
    parser = argparse.ArgumentParser(description="Single-device Sumi StopWatch relay behind an HTTPS gateway")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", type=Path, default=Path("sumi.sqlite3"))
    args = parser.parse_args()
    os.umask(0o077)
    variables = ("SUMI_DEVICE_TOKEN", "SUMI_MCP_TOKEN", "OPENAI_API_KEY", "SUMI_CALLBACK_HOST")
    missing = [name for name in variables if not os.environ.get(name)]
    if missing:
        parser.error("Missing " + ", ".join(missing))
    device_token, mcp_token, audio_key, callback_host = (os.environ[name] for name in variables)
    if len(device_token) < 32 or len(mcp_token) < 32 or device_token == mcp_token:
        parser.error("Use distinct device and MCP tokens of at least 32 characters")
    relay = Relay(
        args.database, device_token, mcp_token,
        Audio(audio_key, os.getenv("SUMI_TTS_VOICE", "marin")), Callback(callback_host),
    )
    stop = threading.Event()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.relay = relay
        worker = threading.Thread(target=relay.worker, args=(stop,), daemon=True)
        worker.start()
        logging.info("Listening on %s:%s", *server.server_address)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
