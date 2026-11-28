"""Receives drive recordings from car_logger.py over your home network. Standard library only.

    python3 log_server.py --dir ~/car-logs --token SECRET [--port 8765]
    SSM_UPLOAD_TOKEN=SECRET python3 log_server.py --dir ~/car-logs

Endpoints (each needs "Authorization: Bearer <token>"):
    GET /health              200 "ok" - the car checks this to see if it's home
    PUT /upload/<name>.csv   gzip-compressed CSV with X-Content-SHA256 (of the uncompressed file);
                             replies {"saved_as": ..., "sha256": ...}

A file is written under a temporary name and renamed when complete, so a half-received upload
never looks like a recording. Sending the same file twice is harmless; a different file with a
name already taken is saved as name_2.csv. Plain HTTP: meant for a home network, not the internet.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\.csv$")
MAX_UPLOAD = 1 << 30            # 1 GB compressed or not: hours of driving at ~45 MB an hour


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Handler(BaseHTTPRequestHandler):
    server_version = "ssm-log-server"
    token: str = ""
    out_dir: Path = Path(".")

    def _reply(self, code: int, body: dict | str) -> None:
        data = (json.dumps(body) if isinstance(body, dict) else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json" if isinstance(body, dict) else "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self) -> bool:
        given = self.headers.get("Authorization", "")
        if hmac.compare_digest(given.encode(), f"Bearer {self.token}".encode()):
            return True
        self._reply(401, "bad or missing token")
        return False

    def do_GET(self) -> None:
        if self.path != "/health":
            return self._reply(404, "not found")
        if self._authorised():
            self._reply(200, "ok")

    def do_PUT(self) -> None:
        m = re.fullmatch(r"/upload/([^/]+)", self.path)
        if not m:
            return self._reply(404, "not found")
        if not self._authorised():
            return
        name = m.group(1)
        if not NAME.match(name):
            return self._reply(400, "file name must be letters, digits, _ . - and end in .csv")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._reply(411, "Content-Length required")
        if not 0 < length <= MAX_UPLOAD:
            return self._reply(413, "too large")
        want = self.headers.get("X-Content-SHA256", "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", want):
            return self._reply(400, "X-Content-SHA256 required")

        tmp = self.out_dir / f".{name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
        try:
            got = self._receive(tmp, length, self.headers.get("Content-Encoding", "") == "gzip")
            if got != want:
                return self._reply(422, "checksum mismatch - the upload was damaged")
            saved = self._place(tmp, name, got)
        except (ValueError, EOFError, OSError, zlib.error) as e:
            return self._reply(400, f"upload failed: {e}")
        finally:
            tmp.unlink(missing_ok=True)
        self.log_message("received %s (%s)", saved.name, f"{saved.stat().st_size / 1e6:.1f} MB")
        self._reply(201, {"saved_as": saved.name, "sha256": got})

    def _receive(self, tmp: Path, length: int, gzipped: bool) -> str:
        """Write the body (uncompressed) to tmp; return its SHA-256."""
        h = hashlib.sha256()
        inflate = zlib.decompressobj(16 + zlib.MAX_WBITS) if gzipped else None
        written, left = 0, length
        with open(tmp, "wb") as f:
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    raise EOFError("connection closed early")
                left -= len(chunk)
                if inflate:
                    chunk = inflate.decompress(chunk, MAX_UPLOAD - written + 1)
                written += len(chunk)
                if written > MAX_UPLOAD:
                    raise ValueError("too large")
                h.update(chunk)
                f.write(chunk)
            if inflate:
                if not inflate.eof:
                    raise ValueError("incomplete gzip data")
            f.flush()
            os.fsync(f.fileno())
        return h.hexdigest()

    def _place(self, tmp: Path, name: str, digest: str) -> Path:
        """Move tmp into place as name, name_2, ... - or keep the existing copy if identical."""
        stem, n = name[:-4], 1
        while True:
            dest = self.out_dir / (name if n == 1 else f"{stem}_{n}.csv")
            if not dest.exists():
                try:
                    os.link(tmp, dest)            # fails if another upload took the name meanwhile
                except FileExistsError:
                    continue
                return dest
            if sha256(dest) == digest:
                return dest
            n += 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Receive drive recordings from car_logger.py")
    ap.add_argument("--dir", type=Path, required=True, help="where to save recordings")
    ap.add_argument("--token", default=os.environ.get("SSM_UPLOAD_TOKEN", ""),
                    help="shared secret (or set SSM_UPLOAD_TOKEN)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    if len(args.token) < 16:
        print("Give a --token of at least 16 characters, e.g.: "
              "python3 -c 'import secrets; print(secrets.token_urlsafe(24))'")
        return 1
    args.dir.mkdir(parents=True, exist_ok=True)
    Handler.token, Handler.out_dir = args.token, args.dir.resolve()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Saving recordings to {Handler.out_dir} - listening on {args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
