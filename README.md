# Fathom webhook — Render deploy

A tiny always-on web server that receives Fathom "meeting content ready" webhooks, verifies the
Svix signature, and pulls out the transcript. Deploying it on Render gives a **permanent URL**, so
you register it in Fathom **once** — no more re-pasting a new trycloudflare URL every session.

## Files
- `fathom_webhook.py` — the server (binds to `$PORT`, which Render injects).
- `fathom_api.py` — Fathom REST client (used as a fallback when a webhook omits the transcript).
- `requirements.txt` — only `requests`.
- `render.yaml` — Render blueprint.

## Deploy (one time)
1. Push this folder to its **own** GitHub repo (private is fine).
2. Render → **New → Web Service** → connect that repo. Render reads `render.yaml`
   (runtime: python · build `pip install -r requirements.txt` · start `python fathom_webhook.py`).
3. In the service's **Environment**, add:
   - `FATHOM_WEBHOOK_SECRET` = the `whsec_…` Fathom shows for this webhook
   - `FATHOM_API_KEY` = your Fathom API key
4. Deploy. Render gives a URL like `https://fathom-webhook.onrender.com`.
5. In **Fathom → Settings → Webhooks**, set the URL to `https://<your-service>.onrender.com/fathom`
   (enable "include transcript"). Visiting the base URL in a browser should print
   `fathom_webhook is up.`

## Known limits of Render's free tier (read before relying on it)
- **Ephemeral disk:** transcripts written to `transcripts/` are **lost on restart/redeploy**. A
  stable URL that *receives* is proven, but to keep transcripts durably the webhook should forward
  each one (e.g. create a ClickUp task / POST to the ops console) — that's the next iteration.
- **Sleeps after ~15 min idle:** the first webhook after idle triggers a cold start (~30–60 s).
  Fathom retries failed deliveries, so it still lands, just not instantly.
- For always-instant + durable, use a paid Render disk, or a named Cloudflare tunnel to an
  always-on box (keeps a fixed hostname, transcripts persist on that box).

## No secrets in this repo
Both scripts read `FATHOM_WEBHOOK_SECRET` / `FATHOM_API_KEY` from the environment only — never
hardcode them here. `.env` is gitignored.
