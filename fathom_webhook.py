#!/usr/bin/env python3
"""
fathom_webhook.py — auto-catch a meeting transcript the moment the meeting ends.

WHAT THIS IS (and why it's different from fathom_api.py)
-------------------------------------------------------
`fathom_api.py` is *pull*: a human runs it AFTER a meeting to fetch a transcript.
This file is *push*: it's a tiny always-listening web server. When a Fathom
meeting finishes, FATHOM itself sends an HTTP POST to us ("new meeting content
ready"). We verify it's genuinely from Fathom, grab the transcript, and save it
to a file — with nobody having to run anything. That's the whole point: the
system already knows when the meeting is over, instead of you telling it.

THE FLOW
--------
    meeting ends
        -> Fathom POSTs a JSON "webhook" to a URL you registered with Fathom
        -> this server receives it at  POST /fathom
        -> we VERIFY the signature (proves the POST really came from Fathom)
        -> we pull the transcript out of the payload (or, if the payload didn't
           include it, fetch it from the API using the recording id)
        -> we save it to  transcripts/<timestamp>-<id>.txt

THE ONE CATCH: A WEBHOOK NEEDS A PUBLIC URL
-------------------------------------------
Your laptop on home wifi has no public address, so Fathom can't reach it directly.
A "tunnel" fixes this: it gives you a temporary public https URL that forwards to
this server. The simplest is ngrok:

    1.  python fathom_webhook.py                 # start this server (listens on :8000)
    2.  ngrok http 8000                          # in a 2nd terminal -> prints an https URL
    3.  copy that https URL, add "/fathom" to it, e.g.
            https://abc123.ngrok-free.app/fathom
    4.  Fathom -> Settings -> Webhooks -> add that URL, enable "include transcript".
        Fathom shows you a signing secret that starts with  whsec_  -> copy it.
    5.  put that secret in ~/.hermes/.env:
            FATHOM_WEBHOOK_SECRET=whsec_...
        (Registering a NEW webhook gives a NEW secret — which conveniently
         retires any old secret that may have leaked.)

It only works while BOTH this server and the tunnel are running. An always-on
machine (a small cloud box / your Unraid server) is the later upgrade — the code
is identical, only the URL becomes permanent.

SECURITY: WHY WE VERIFY THE SIGNATURE
-------------------------------------
The URL is public, so *anyone* could POST junk to it and trick us into saving a
fake transcript. Fathom prevents that by signing every webhook (Svix scheme):
it sends three headers — webhook-id, webhook-timestamp, webhook-signature — and
signs "id.timestamp.body" with your secret using HMAC-SHA256. We recompute that
signature with the same secret; if ours doesn't match theirs, we reject the POST.
Only someone holding your secret can forge a valid signature, so this proves the
sender is really Fathom. (This is why the secret must stay secret.)

NO SECRET IN THIS FILE
----------------------
The signing secret is read from FATHOM_WEBHOOK_SECRET (env or ~/.hermes/.env),
never hardcoded, never committed.

USAGE
-----
    python fathom_webhook.py --selftest      # prove the signature crypto works (no network)
    python fathom_webhook.py                 # run the server on port 8000
    python fathom_webhook.py --port 9000     # run on a different port
    python fathom_webhook.py --insecure      # skip signature check (LOCAL TESTING ONLY)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Reuse everything we already built and proved in fathom_api.py:
#   - load_key_from_env_file(): pulls FATHOM_API_KEY out of ~/.hermes/.env
#   - fetch_transcript(): the API fallback if the webhook didn't include the text
#   - _format_segments(): turns a [{speaker,text,timestamp}, ...] list into clean lines
from fathom_api import _format_segments, fetch_transcript, load_key_from_env_file

# Force UTF-8 output so printing a transcript line on Windows never crashes (cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ENV_PATH = Path.home() / ".hermes" / ".env"
# Where saved transcripts and raw payloads land (created on first use).
OUT_DIR = Path(__file__).resolve().parent / "transcripts"
# Reject webhooks whose timestamp is older/newer than this many seconds (replay guard).
TIMESTAMP_TOLERANCE_S = 5 * 60


# ---------------------------------------------------------------------------
# Secret loading
# ---------------------------------------------------------------------------
def load_webhook_secret() -> str | None:
    """Return FATHOM_WEBHOOK_SECRET from the environment, or from ~/.hermes/.env.

    Mirrors fathom_api.load_key_from_env_file but for the webhook secret. We read
    only the one line we need and never print its value.
    """
    secret = os.environ.get("FATHOM_WEBHOOK_SECRET")
    if secret:
        return secret
    if not ENV_PATH.exists():
        return None
    for line in ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("FATHOM_WEBHOOK_SECRET") and "=" in line:
            return line.partition("=")[2].strip().strip('"').strip("'")
    return None


# ---------------------------------------------------------------------------
# Signature verification (Svix scheme — what Fathom uses)
# ---------------------------------------------------------------------------
def compute_signature(secret: str, msg_id: str, timestamp: str, body: bytes) -> str:
    """Compute the expected base64 HMAC-SHA256 signature for one webhook.

    The signed content is exactly the bytes "id.timestamp.body" (periods between).
    The signing key is the base64-decoded part of the secret AFTER the 'whsec_'
    prefix. We HMAC-SHA256 the signed content with that key and base64-encode it.
    This must match what Fathom computed on their side.
    """
    secret_b64 = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
    key = base64.b64decode(secret_b64)
    signed_content = b"%s.%s.%s" % (msg_id.encode(), timestamp.encode(), body)
    digest = hmac.new(key, signed_content, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify_signature(secret: str, headers, body: bytes) -> tuple[bool, str]:
    """Return (ok, reason). ok=True only if the POST is a genuine, fresh Fathom webhook.

    Fathom (Svix) sends headers webhook-id / webhook-timestamp / webhook-signature.
    Some setups use the svix-* prefix instead, so we accept either spelling.
    The signature header is a space-separated list like "v1,<sigA> v1,<sigB>"; we
    pass if ANY entry matches our computed signature (constant-time compare).
    """
    msg_id = headers.get("webhook-id") or headers.get("svix-id")
    timestamp = headers.get("webhook-timestamp") or headers.get("svix-timestamp")
    sig_header = headers.get("webhook-signature") or headers.get("svix-signature")
    if not (msg_id and timestamp and sig_header):
        return False, "missing webhook-id/timestamp/signature header(s)"

    # Replay guard: a captured-and-resent webhook will have an old timestamp.
    try:
        age = abs(time.time() - int(timestamp))
        if age > TIMESTAMP_TOLERANCE_S:
            return False, f"timestamp too far off ({int(age)}s) — possible replay"
    except ValueError:
        return False, "non-numeric webhook-timestamp"

    expected = compute_signature(secret, msg_id, timestamp, body)
    # Header entries look like "v1,<base64sig>"; compare the part after the comma.
    for entry in sig_header.split():
        _, _, their_sig = entry.partition(",")
        if their_sig and hmac.compare_digest(their_sig, expected):
            return True, "ok"
    return False, "signature mismatch — sender is not holding your secret"


# ---------------------------------------------------------------------------
# Pulling the transcript out of whatever shape Fathom sends
# ---------------------------------------------------------------------------
def _deep_find(obj, keys: set[str]):
    """Depth-first search a nested dict/list for the first value under any of `keys`.

    Fathom's exact field names for the recording id / transcript aren't pinned down
    in the public docs, so instead of hardcoding one path we hunt for the usual
    names anywhere in the payload. The raw payload is also dumped to disk (see
    handler) so the real shape can be confirmed and this tightened if needed.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and v not in (None, "", [], {}):
                return v
        for v in obj.values():
            found = _deep_find(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_find(item, keys)
            if found is not None:
                return found
    return None


def extract_transcript_text(payload: dict) -> str | None:
    """Best-effort: get formatted transcript text directly from the webhook payload.

    If the webhook was configured with "include transcript", the text is already in
    the body — no API call needed. The transcript may arrive as:
      * a list of segments [{speaker, text, timestamp}, ...]  -> format like the API
      * a plain string / {"plaintext": "..."} / {"text": "..."} -> use as-is
    Returns None if no transcript is present (caller then falls back to the API).
    """
    transcript = _deep_find(payload, {"transcript"})
    if transcript is None:
        # Sometimes the segments live under a different name.
        transcript = _deep_find(payload, {"segments", "messages"})
    if transcript is None:
        return None

    if isinstance(transcript, str):
        return transcript
    if isinstance(transcript, dict):
        return transcript.get("plaintext") or transcript.get("text")
    if isinstance(transcript, list) and transcript:
        try:
            return _format_segments(transcript)  # expects speaker/text/timestamp
        except (KeyError, TypeError):
            # Unexpected segment shape — hand back the raw JSON so nothing is lost.
            return json.dumps(transcript, indent=2, ensure_ascii=False)
    return None


def extract_recording_id(payload: dict) -> int | None:
    """Find the recording id in the payload so we can fall back to the API if the
    transcript wasn't included. Tries the names fathom_api already knows about."""
    rid = _deep_find(payload, {"recording_id", "id"})
    try:
        return int(rid) if rid is not None else None
    except (ValueError, TypeError):
        return None


def handle_payload(payload: dict, raw_body: bytes) -> Path | None:
    """Save the transcript for one webhook. Returns the file path written, or None.

    1. Always dump the raw payload (so the exact field shape is inspectable once).
    2. Try to read the transcript straight from the payload.
    3. If it isn't there, use the recording id + the API to fetch it.
    4. Write the transcript to transcripts/<utc-time>-<id>.txt and return its path.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())

    # 1. Raw payload, for debugging the real field names on the very first real fire.
    (OUT_DIR / f"{stamp}-payload.json").write_bytes(raw_body)

    rid = extract_recording_id(payload)
    title = _deep_find(payload, {"title", "meeting_title", "name"}) or "meeting"
    share = _deep_find(payload, {"share_url", "url", "recording_url"})

    # 2. Prefer the transcript that's already in the webhook body.
    text = extract_transcript_text(payload)
    source = "webhook payload"

    # 3. Fall back to the API if the body didn't carry the transcript.
    if not text and rid is not None:
        try:
            load_key_from_env_file()
            text = fetch_transcript(rid).text
            source = "Fathom API"
        except Exception as e:
            print(f"  ! API fallback failed: {e}", file=sys.stderr)

    if not text:
        print("  ! no transcript found in payload and no API fallback worked.")
        return None

    out_path = OUT_DIR / f"{stamp}-{rid or 'unknown'}.txt"
    header = f"# {title}\n# recording_id={rid}  source={source}\n"
    if share:
        header += f"# share={share}\n"
    out_path.write_text(header + "\n" + text, encoding="utf-8")
    print(f"  -> saved transcript ({len(text)} chars, via {source}) to {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# The HTTP server
# ---------------------------------------------------------------------------
class FathomWebhookHandler(BaseHTTPRequestHandler):
    # These are set on the server object in run_server() and read via self.server.
    secret: str | None = None
    insecure: bool = False

    def _reply(self, code: int, message: str) -> None:
        body = message.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # A plain browser visit / health check — handy to confirm the tunnel works.
        self._reply(200, "fathom_webhook is up. POST your Fathom webhook to /fathom\n")

    def do_POST(self):
        # Only accept the path we told Fathom to use; ignore stray bots hitting "/".
        if self.path.rstrip("/") not in ("/fathom", ""):
            self._reply(404, "not found\n")
            return

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)  # RAW bytes — needed exactly for the signature

        secret = self.server.secret  # type: ignore[attr-defined]
        if self.server.insecure:  # type: ignore[attr-defined]
            print("! --insecure: skipping signature check (do not use in production)")
        elif not secret:
            self._reply(500, "server has no FATHOM_WEBHOOK_SECRET configured\n")
            print("! refused webhook: no secret configured to verify it.")
            return
        else:
            ok, reason = verify_signature(secret, self.headers, raw_body)
            if not ok:
                self._reply(401, f"signature check failed: {reason}\n")
                print(f"! rejected webhook: {reason}")
                return

        # Parse and process. We reply 200 fast even if saving has a hiccup, so Fathom
        # doesn't keep retrying a delivery we've already accepted.
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self._reply(400, "body is not valid JSON\n")
            return

        print(f"\n[{time.strftime('%H:%M:%S')}] webhook received — processing...")
        try:
            handle_payload(payload, raw_body)
        except Exception as e:
            print(f"  ! error while handling payload: {e}", file=sys.stderr)
        self._reply(200, "ok\n")

    def log_message(self, fmt, *args):
        # Quieten the default one-line-per-request noise; we print our own status.
        pass


def run_server(port: int, secret: str | None, insecure: bool) -> int:
    server = ThreadingHTTPServer(("0.0.0.0", port), FathomWebhookHandler)
    server.secret = secret  # type: ignore[attr-defined]
    server.insecure = insecure  # type: ignore[attr-defined]
    mode = "INSECURE (no signature check)" if insecure else "verifying signatures"
    print(f"fathom_webhook listening on http://0.0.0.0:{port}/fathom  [{mode}]")
    print("Expose it publicly with:  ngrok http", port)
    print("Then register  https://<your-ngrok>.app/fathom  in Fathom -> Webhooks.")
    print("Ctrl-C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


# ---------------------------------------------------------------------------
# Self-test: prove the signature math without any network
# ---------------------------------------------------------------------------
def selftest() -> int:
    """Sign a fake payload with a known secret, then confirm verify_signature
    ACCEPTS the correct signature and REJECTS a tampered body. No network, no key."""
    secret = "whsec_" + base64.b64encode(b"test-signing-key-32bytes-padding!").decode()
    body = b'{"transcript":[{"speaker":{"display_name":"Alice"},"text":"hi","timestamp":"00:00:01"}]}'
    msg_id = "msg_2abc"
    timestamp = str(int(time.time()))
    good_sig = compute_signature(secret, msg_id, timestamp, body)

    class H:  # minimal stand-in for the headers object
        def __init__(self, d):
            self._d = d

        def get(self, k, default=None):
            return self._d.get(k, default)

    good_headers = H(
        {
            "webhook-id": msg_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": f"v1,{good_sig}",
        }
    )
    ok_good, _ = verify_signature(secret, good_headers, body)
    ok_tampered, _ = verify_signature(secret, good_headers, body + b" tampered")

    print("Valid signature accepted: ", ok_good)
    print("Tampered body rejected:   ", not ok_tampered)

    # Also prove the transcript extractor reads the inline payload.
    text = extract_transcript_text(json.loads(body))
    print("Inline transcript parsed: ", text == "00:00:01 Alice: hi")

    passed = ok_good and not ok_tampered and text == "00:00:01 Alice: hi"
    print("\nSELF-TEST:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Receive Fathom webhooks and auto-save transcripts.")
    ap.add_argument("--selftest", action="store_true", help="prove the signature crypto (no network)")
    ap.add_argument("--port", type=int, default=8000, help="port to listen on (default 8000)")
    ap.add_argument("--insecure", action="store_true", help="skip signature check (LOCAL TESTING ONLY)")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    # Cloud hosts (Render, Railway, etc.) inject the port to bind on via $PORT; it takes
    # precedence over --port so the same file runs locally and in the cloud unchanged.
    port = int(os.environ.get("PORT", args.port))

    secret = load_webhook_secret()
    if not secret and not args.insecure:
        print(
            "No FATHOM_WEBHOOK_SECRET found. Set it as an environment variable (Render/Railway\n"
            "dashboard) or in ~/.hermes/.env locally:  FATHOM_WEBHOOK_SECRET=whsec_...\n"
            "(or run with --insecure to test locally without verification)",
            file=sys.stderr,
        )
        return 1
    return run_server(port, secret, args.insecure)


if __name__ == "__main__":
    sys.exit(main() or 0)
