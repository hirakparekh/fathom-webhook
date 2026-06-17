# Handoff — Fathom meeting-capture pipeline

**From:** Hirak (frontend) · **To:** backend dev
**Goal:** every finished Fathom meeting → a ClickUp task with the minutes, automatically.

This repo captures the **transcript half**. The **processing half** (transcript → minutes →
ClickUp) is what's left to build — that's your task. Read the "What to build" section; the rest
is context.

---

## TL;DR

- A webhook is **deployed and live** on Render (permanent URL) and **receives** Fathom's
  "meeting ended" events.
- A pull client (`fathom_api.py`) **reliably fetches** any transcript by recording id. This is
  the dependable path and is what we've actually been using.
- **Missing:** a backend job that takes each new transcript and turns it into a **ClickUp task**
  (the Agent OS "Track A" loop). **Recommended shape: a scheduled pull job that runs where the
  agent (Hermes) lives — not more webhook plumbing.** Reasoning below.

---

## What's in this repo

| File | What it is |
|---|---|
| `fathom_webhook.py` | The Render webhook receiver. Verifies Fathom's Svix signature, pulls the transcript out of the payload (or falls back to the API), saves it. Binds `$PORT`. |
| `fathom_api.py` | Fathom REST client. `--list` = recent recordings + ids; `--recording-id <id>` = fetch + print/save a transcript. **The reliable fetch path.** |
| `send_test_webhook.py` | Fires a correctly-signed test webhook at a locally-running `fathom_webhook.py`, to prove the pipeline without a real meeting. |
| `render.yaml`, `requirements.txt` | Deploy config (Render blueprint; only dep is `requests`). |

No secrets are committed — keys are read from environment variables only.

## What's already deployed & working

- **Render web service** `fathom-webhook` (free tier) → **`https://fathom-webhook.onrender.com`**
  (POST path `/fathom`, also accepts `/`).
- Registered in **Fathom → Settings → Webhooks** (Transcript event, "include transcript" on).
- Env on Render: `FATHOM_WEBHOOK_SECRET` (the `whsec_…` from Fathom) and `FATHOM_API_KEY`.
- Verified: a signed `POST` returns `200 ok`; unsigned returns `401`; the API pull reliably
  returns full transcripts.

---

## The key architectural decision (please read)

**The webhook receives but cannot deliver.** On Render's free tier it writes the transcript to
its **own ephemeral disk** (lost on restart/sleep) and there is **no step that forwards it
anywhere**. So the webhook, today, doesn't get the transcript to anyone.

Two ways to close the gap:

- **(A) Scheduled PULL — recommended.** A job runs every few minutes, checks
  `fathom_api.py --list` for a new recording id, pulls it, and processes it. Durable, simple, no
  signature/replay handling, no ephemeral-disk problem. Run it **where the agent lives**.
- **(B) Make the webhook FORWARD.** Add a "push to ClickUp/agent" step on receipt. Real-time, but
  inherits the free-tier sleep + ephemeral quirks, **and** the processing brain (Hermes) runs on
  a laptop/Unraid with no public address — so a cloud webhook calling back into it is awkward
  (the same "no public URL" problem the webhook itself was built to dodge).

**Why A:** the "transcript → ClickUp task" brain is the **Agent OS agent pipeline** (Hermes +
the synthesis skill + the `clickup/` client), which runs where Hermes runs. Put the *fetch* and
the *processing* together there. The Render webhook's best role shrinks to a thin
**trigger/notifier** ("a meeting just finished"); when the agent pipeline itself is later deployed
to the cloud, option B becomes clean.

---

## What to build (your task)

The Track A loop: **new transcript → synthesized minutes → ClickUp task.**

1. **Detect + fetch new transcripts.** Reuse `fathom_api.py`: `--list` to find new recording
   ids, `--recording-id <id>` to fetch. Persist the last-seen id so you don't reprocess.
   (Fathom transcripts lag a few minutes after a meeting ends — poll/retry.)
2. **Synthesize minutes.** `## Summary` / `## Decisions` / `## Action items`. Per the Agent OS
   plan this is a Hermes skill (`skills/meeting-to-task/SKILL.md`); or do it with a direct LLM
   call. Don't invent action items not in the transcript.
3. **Create the ClickUp task.** Use the **typed client `clickup/client.py`** in the agent-os repo
   — **all ClickUp writes go through it (ADR-0001).** `create_task(list_id, name,
   markdown_description)` and `add_comment(task_id, text)` already exist.
4. **Schedule it.** Cron / Windows Task Scheduler / Render Cron Job, running where Hermes/the
   agent lives. Optionally let the deployed webhook be the trigger instead of a timer.

### Pointers in the main repo — `github.com/fenilgtm/agent-os`
- `docs/superpowers/plans/2026-06-07-track-a-copilot-loop.md` — the full Track A plan (this is the
  blueprint for exactly this work; note it originally assumed a Slack-triggered pull — the Render
  webhook is an add-on trigger we built on top).
- `clickup/client.py` — the typed ClickUp client (use this, not raw HTTP).
- `skills/` — where the synthesis skill belongs.

---

## Run / test locally

```bash
# Prove the signature crypto (no network, no keys):
python fathom_webhook.py --selftest

# Run the receiver locally (needs FATHOM_WEBHOOK_SECRET in env or ~/.hermes/.env):
python fathom_webhook.py                 # terminal 1
python send_test_webhook.py              # terminal 2 — fires a signed test

# Pull a real transcript by id (needs FATHOM_API_KEY):
python fathom_api.py --list
python fathom_api.py --recording-id <id> --out transcript.txt
```

## Secrets & constraints

- **Env only** — `FATHOM_API_KEY`, `FATHOM_WEBHOOK_SECRET`, and for processing `CLICKUP_TOKEN` +
  the target list id. Never commit them. Locally they live in `~/.hermes/.env`.
- **Render free tier:** sleeps after ~15 min idle (cold start ~30–60 s; Fathom retries), ephemeral
  disk. Good as a trigger, not for storage.
- **Ignore `meet_capture.py`** (a separate, abandoned Google-Meet-caption scraper) — Meet's HTML
  broke it; the Fathom path above is the one to build on.
