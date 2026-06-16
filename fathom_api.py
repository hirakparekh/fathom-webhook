#!/usr/bin/env python3
"""
fathom_api.py — fetch the RAW transcript of a meeting from the Fathom API.

WHAT THIS IS
------------
A small, self-contained client for Fathom's REST API. You give it a meeting's
`recording_id`; it returns that meeting's full word-for-word transcript, formatted
one line per spoken segment:

    00:00:15 Hirak Parekh: Okay, let's see.
    00:00:17 Hirak Parekh: The meeting is being recorded.

This is the exact shape the Agent OS repo's Track A / Task 2 spec calls for
(`tools/fathom/client.py`): authenticate to the Fathom API, fetch a transcript by id,
return structured transcript text. The agent then acts on that text — this client
does NOT summarize (that's a separate synthesis step) and never writes to ClickUp.

HOW FATHOM FITS IN
------------------
Fathom records + transcribes your meetings (you must be present; its desktop app /
notetaker captures the call). Afterwards the transcript is available via this API.
The transcript itself is free/unlimited on Fathom; *API access* is a Premium feature
(usable during Fathom's 30-day free preview).

AUTH (no secret in this file!)
------------------------------
The API key is read from the environment variable FATHOM_API_KEY, or from
~/.hermes/.env. It is NEVER hardcoded here and NEVER committed.
Get a key from Fathom → Settings → API Access → "Add", then put it in ~/.hermes/.env:

    FATHOM_API_KEY=your_key_here

USAGE
-----
    python fathom_api.py --selftest            # prove the parsing works (no key, no network)
    python fathom_api.py --list                # list your recent meetings + their recording ids
    python fathom_api.py --recording-id 12345  # fetch + print that meeting's raw transcript
    python fathom_api.py --recording-id 12345 --out transcript.txt   # also save to a file
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# On Windows the default console encoding (cp1252) crashes on emoji / non-ASCII in a
# transcript. Force UTF-8 so printing a transcript never blows up.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Where we look for the API key if it isn't already an environment variable.
ENV_PATH = Path.home() / ".hermes" / ".env"

# --- Fathom API constants (from the repo's Track A / Task 2 spec) ---
# Base URL for Fathom's "external" REST API. Every endpoint hangs off this.
FATHOM_BASE = "https://api.fathom.ai/external/v1"
# Don't let a single HTTP call hang forever — give up after 30 seconds.
TIMEOUT_S = 30


@dataclass(frozen=True)
class Transcript:
    """A fetched transcript. `frozen=True` makes it read-only once created, so a
    transcript can't be accidentally mutated after we hand it back."""

    recording_id: int          # which Fathom recording this came from
    text: str                  # the full transcript, "HH:MM:SS Speaker: text" per line


def _format_segments(segments) -> str:
    """Turn Fathom's list of transcript segments into one clean block of text.

    Fathom returns the transcript as a list of segments, each shaped like:
        { "speaker": {"display_name": "Alice", ...},
          "text": "Let's ship the POC.",
          "timestamp": "00:05:32" }

    We render each segment as a single line "timestamp Speaker: text" and join them
    with newlines. This is the repo's required output format.
    """
    return "\n".join(
        f"{seg['timestamp']} {seg['speaker']['display_name']}: {seg['text']}"
        for seg in segments
    )


def fetch_transcript(recording_id: int, api_key: str | None = None) -> Transcript:
    """Fetch one meeting's transcript from Fathom and return it as a `Transcript`.

    Steps:
      1. Get the API key (passed in, or from the environment).
      2. GET the recording's /transcript endpoint, sending the key in the X-Api-Key
         header (this is how Fathom authenticates the request).
      3. Raise if the HTTP call failed (bad key, missing recording, network, etc.).
      4. Pull the "transcript" array out of the JSON and format it into text.
    """
    api_key = api_key or os.environ["FATHOM_API_KEY"]
    import requests  # imported lazily so --selftest works even without `requests`

    response = requests.get(
        f"{FATHOM_BASE}/recordings/{recording_id}/transcript",
        headers={"X-Api-Key": api_key},          # <-- the API key travels here
        timeout=TIMEOUT_S,
    )
    response.raise_for_status()                  # turns a 4xx/5xx into a clear error
    segments = response.json()["transcript"]     # the diarized transcript list
    return Transcript(recording_id=recording_id, text=_format_segments(segments))


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------
def load_key_from_env_file() -> None:
    """If FATHOM_API_KEY isn't already set in the environment, try to read it out of
    ~/.hermes/.env. We parse only the one line we need and never print its value."""
    if os.environ.get("FATHOM_API_KEY") or not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("FATHOM_API_KEY") and "=" in line:
            # take everything after the first "=", strip surrounding quotes/space
            os.environ["FATHOM_API_KEY"] = line.partition("=")[2].strip().strip('"').strip("'")


# The repo's own test fixture (Track A / Task 2, Step 1). --selftest formats this
# fake response so we can confirm the parsing logic works WITHOUT a key or network.
SAMPLE_RESPONSE = {
    "transcript": [
        {
            "speaker": {"display_name": "Alice", "matched_calendar_invitee_email": "a@x.com"},
            "text": "Let's ship the POC.",
            "timestamp": "00:05:32",
        },
        {
            "speaker": {"display_name": "Usman", "matched_calendar_invitee_email": "u@x.com"},
            "text": "Agreed, merging the harness.",
            "timestamp": "00:06:01",
        },
    ]
}


def selftest() -> int:
    """Format the sample response and check the first line matches what we expect.
    Proves the parsing/formatting is correct without touching Fathom at all."""
    text = _format_segments(SAMPLE_RESPONSE["transcript"])
    expected_first = "00:05:32 Alice: Let's ship the POC."
    print("Formatted transcript from the sample response:\n")
    print(text)
    print()
    ok = text.splitlines()[0] == expected_first
    print("SELF-TEST:", "PASS - the client logic works." if ok else "FAIL")
    return 0 if ok else 1


def _auth_get(path: str, params: dict | None = None):
    """GET a Fathom API path with auth, trying both documented auth styles.

    Fathom's docs mention an X-Api-Key header AND a Bearer token. We try X-Api-Key
    first; if that returns 401 (unauthorized) we retry with a Bearer header, so this
    works regardless of which scheme the account expects.
    """
    import requests

    key = os.environ["FATHOM_API_KEY"]
    url = f"{FATHOM_BASE}{path}"
    resp = None
    for headers in ({"X-Api-Key": key}, {"Authorization": f"Bearer {key}"}):
        resp = requests.get(url, headers=headers, params=params or {}, timeout=TIMEOUT_S)
        if resp.status_code != 401:   # not an auth failure -> this header worked, stop
            return resp
    return resp                       # both failed; caller reports the error


def list_meetings_cmd(limit: int = 25) -> int:
    """List recent meetings and their recording ids (GET /meetings).

    Use this to find the `recording_id` you then pass to --recording-id. Fathom's
    field names vary a little, so we look for the id/title under several possible keys
    rather than assuming one exact shape.
    """
    resp = _auth_get("/meetings", {"limit": limit})
    if resp.status_code != 200:
        # Surface the real error (e.g. 401 bad key, 403 no API access) so it's obvious.
        raise SystemExit(f"Fathom API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    # The list of meetings lives under "items" (seen in practice) but be defensive.
    items = ((data.get("items") or data.get("meetings") or data.get("data"))
             if isinstance(data, dict) else data) or []

    if not items:
        # No error means the KEY is valid — there just aren't any recordings yet.
        keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
        print("Key WORKS (no auth error), but no meetings returned yet.")
        print("Response shape:", keys)
        return 0

    print(f"{len(items)} meeting(s):\n")
    for m in items:
        rid = m.get("recording_id") or m.get("id") or (m.get("recording") or {}).get("id")
        title = m.get("title") or m.get("meeting_title") or m.get("name") or "(untitled)"
        when = (m.get("created_at") or m.get("recording_start_time")
                or m.get("scheduled_start_time") or "")
        print(f"  id={rid}   {str(when):25.25}  {title}")
    print("\nThen:  python fathom_api.py --recording-id <id>")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch a raw meeting transcript from the Fathom API.")
    ap.add_argument("--selftest", action="store_true", help="prove parsing works (no key/network)")
    ap.add_argument("--list", action="store_true", help="list your recent meetings + recording IDs")
    ap.add_argument("--recording-id", type=int, help="Fathom recording id to fetch (needs key)")
    ap.add_argument("--out", help="write the transcript to this file as well")
    args = ap.parse_args()

    # --selftest needs neither a key nor the network, so handle it before anything else.
    if args.selftest:
        return selftest()

    # Everything below talks to Fathom, so we need the API key first.
    load_key_from_env_file()
    if not os.environ.get("FATHOM_API_KEY"):
        raise SystemExit(
            "No FATHOM_API_KEY. Add it to ~/.hermes/.env:  FATHOM_API_KEY=...\n"
        )

    if args.list:
        return list_meetings_cmd()

    if args.recording_id is None:
        ap.error("pass --selftest, --list, or --recording-id N")

    # Fetch the real transcript and print it; optionally also save to a file.
    try:
        t = fetch_transcript(args.recording_id)
    except Exception as e:
        raise SystemExit(f"Fathom API call failed: {e}")

    print(t.text)
    if args.out:
        Path(args.out).write_text(t.text, encoding="utf-8")
        print(f"\n(wrote {len(t.text)} chars -> {args.out})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
