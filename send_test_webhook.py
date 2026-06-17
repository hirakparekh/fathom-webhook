#!/usr/bin/env python3
"""
send_test_webhook.py — fire a fake, *correctly-signed* Fathom webhook at the
locally-running fathom_webhook.py, to prove the whole pipeline works without
needing a real meeting.

It reads the signing secret from ~/.hermes/.env (never from the command line, so
the secret never lands in your shell history or this chat), builds a realistic
"new meeting content ready" payload, signs it the exact way Fathom does, and
POSTs it to http://localhost:8000/fathom.

If everything is wired correctly you'll see the server print
"webhook received — processing..." and a new file appear in transcripts/.

USAGE
-----
    python fathom_webhook.py        # terminal 1: start the server first
    python send_test_webhook.py     # terminal 2: fire this test
"""

from __future__ import annotations

import json
import time
import urllib.request

# Reuse the SAME signing + secret-loading code the server uses, so this test
# proves the real code path rather than a reimplementation of it.
from fathom_webhook import compute_signature, load_webhook_secret

URL = "http://localhost:8000/fathom"

# A payload shaped like Fathom's "new meeting content ready" event, with the
# transcript included inline (because we enabled "include transcript").
PAYLOAD = {
    "recording_id": 999999,
    "title": "LOCAL TEST — webhook pipeline check",
    "share_url": "https://fathom.video/share/LOCALTEST",
    "transcript": [
        {"speaker": {"display_name": "Hirak"}, "text": "Testing the webhook pipeline.", "timestamp": "00:00:01"},
        {"speaker": {"display_name": "Notetaker"}, "text": "Received and saved automatically.", "timestamp": "00:00:04"},
    ],
}


def main() -> int:
    secret = load_webhook_secret()
    if not secret:
        print("No FATHOM_WEBHOOK_SECRET in ~/.hermes/.env — can't sign the test.")
        return 1

    body = json.dumps(PAYLOAD).encode("utf-8")
    msg_id = "msg_localtest"
    timestamp = str(int(time.time()))
    signature = compute_signature(secret, msg_id, timestamp, body)

    request = urllib.request.Request(
        URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            # The three headers Fathom (Svix) sends. Format matches the real thing.
            "webhook-id": msg_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": f"v1,{signature}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            print(f"server replied {resp.status}: {resp.read().decode().strip()}")
            print("Check the transcripts/ folder and the server terminal.")
            return 0
    except urllib.error.HTTPError as e:
        print(f"server rejected it ({e.code}): {e.read().decode().strip()}")
        return 1
    except urllib.error.URLError as e:
        print(f"could not reach the server at {URL} — is fathom_webhook.py running? ({e})")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
