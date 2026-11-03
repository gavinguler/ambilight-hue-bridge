"""Minimal JointSpace v6 client: one persistent HTTPS connection with digest auth.

The TV's TLS stack stalls when several new connections are opened in quick
succession, so we keep a single keep-alive connection and reconnect lazily.
"""
import hashlib
import http.client
import json
import os
import re
import ssl
import time


class TVClient:
    def __init__(self, ip, user, password, timeout=5):
        self.ip, self.user, self.password, self.timeout = ip, user, password, timeout
        self.ctx = ssl._create_unverified_context()  # self-signed TV certificate
        self.conn = None
        self.challenge = None  # (realm, nonce, qop, opaque)
        self.nc = 0

    def _connect(self):
        self.close()
        self.conn = http.client.HTTPSConnection(self.ip, 1926, timeout=self.timeout, context=self.ctx)

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def _auth_header(self, method, uri):
        realm, nonce, qop, opaque = self.challenge
        self.nc += 1
        nc = f"{self.nc:08x}"
        cnonce = os.urandom(8).hex()
        ha1 = hashlib.md5(f"{self.user}:{realm}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        if qop:
            resp = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
            h = (f'Digest username="{self.user}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
                 f'qop=auth, nc={nc}, cnonce="{cnonce}", response="{resp}"')
        else:
            resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            h = f'Digest username="{self.user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp}"'
        if opaque:
            h += f', opaque="{opaque}"'
        return h + ", algorithm=MD5"

    def _parse_challenge(self, header):
        fields = dict(re.findall(r'(\w+)="?([^",]*)"?', header))
        self.challenge = (fields.get("realm", ""), fields["nonce"], fields.get("qop"), fields.get("opaque"))
        self.nc = 0

    def request(self, method, path, body=None, _retry=True):
        uri = f"/6/{path}"
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(3):
            try:
                if self.conn is None:
                    self._connect()
                headers = {"Content-Type": "application/json"} if data else {}
                if self.challenge:
                    headers["Authorization"] = self._auth_header(method, uri)
                self.conn.request(method, uri, body=data, headers=headers)
                resp = self.conn.getresponse()
                payload = resp.read()
                if resp.status == 401:
                    self._parse_challenge(resp.getheader("WWW-Authenticate", ""))
                    continue
                if resp.status >= 400:
                    raise RuntimeError(f"{method} {uri} -> HTTP {resp.status}: {payload[:200]!r}")
                return json.loads(payload) if payload.strip() else {}
            except (OSError, http.client.HTTPException):
                self.close()
                if attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"{method} {uri}: authentication failed")

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body):
        return self.request("POST", path, body)
