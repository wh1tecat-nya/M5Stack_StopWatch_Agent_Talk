import base64
import hashlib
import hmac
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit

from .audio import encode_json


class CallbackError(Exception):
    pass


class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, address):
        super().__init__(hostname, 443, timeout=8)
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class Callback:
    def __init__(self, host):
        self.host = host

    def validate(self, url):
        parsed = urlsplit(url)
        if (not self.host or parsed.scheme != "https" or parsed.hostname != self.host
                or parsed.netloc not in (self.host, self.host + ":443") or parsed.fragment
                or len(url) > 2048):
            raise CallbackError("invalid_destination")
        return parsed

    def post(self, url, keys, subscription_id, event_id, data):
        parsed = self.validate(url)
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(entry[4][0]) for entry in addresses]
        if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
            raise CallbackError("non_public_destination")

        body = encode_json(data)
        timestamp = str(int(time.time()))
        signing_input = f"{event_id}.{timestamp}.".encode() + body
        signatures = [
            "v1," + base64.b64encode(hmac.digest(key, signing_input, hashlib.sha256)).decode()
            for key in keys
        ]
        path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
        connection = PinnedConnection(parsed.hostname, str(ips[0]))
        try:
            connection.request("POST", path, body, {
                "Content-Type": "application/json",
                "webhook-id": event_id,
                "webhook-timestamp": timestamp,
                "webhook-signature": " ".join(signatures),
                "X-MCP-Subscription-Id": subscription_id,
            })
            response = connection.getresponse()
            return response.status, response.read(8193)
        finally:
            connection.close()
