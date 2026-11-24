"""Download RomRaider's logger definitions (log_defs.xml) into definitions/.

    .venv/bin/python fetch_defs.py

The file comes from the RomRaider project (https://github.com/RomRaider/RomRaider),
which is licensed under the GNU GPL v2 or later. It is not part of this repository and
is fetched from a fixed RomRaider commit, checked against a known SHA-256, so every
install gets the exact file this tool was tested with.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request

import defs

ROMRAIDER_COMMIT = "d37c5eb786f3d2bc4b9353ffa859763f4ba596a5"
URL = (f"https://raw.githubusercontent.com/RomRaider/RomRaider/{ROMRAIDER_COMMIT}"
       "/definitions/log_defs.xml")
SHA256 = "bb371ef1e6e956cb5081b12abe840eea1c6a00ce4c59fb790938c705433ef4e1"


def main() -> int:
    dest = defs.DOWNLOADED_DEFS
    if dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == SHA256:
        print(f"Already up to date: {dest}")
        return 0
    print(f"Downloading {URL}")
    try:
        with urllib.request.urlopen(URL, timeout=30) as r:
            data = r.read()
    except OSError as e:
        print(f"Download failed: {e}", file=sys.stderr)
        return 1
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        print(f"Checksum mismatch (got {digest}); not saving.", file=sys.stderr)
        return 1
    dest.parent.mkdir(exist_ok=True)
    dest.write_bytes(data)
    print(f"Saved {dest} ({len(data) // 1024} KB, {len(defs.load(dest))} SSM base parameters)")
    print("RomRaider is GPL-licensed: https://github.com/RomRaider/RomRaider")
    return 0


if __name__ == "__main__":
    sys.exit(main())
