<div align="center">

# 📡 Fathom Webhook

<img src="https://readme-typing-svg.demolab.com?font=Fira+Code&weight=600&size=22&pause=1000&color=8A2BE2&center=true&vCenter=true&width=620&lines=One+permanent+URL+for+Fathom+meeting+webhooks.;Verifies+the+Svix+signature.;Pulls+out+the+transcript+automatically." alt="Typing SVG" />

<p>
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/Render-000000?logo=render&logoColor=white" />
  <img src="https://img.shields.io/badge/Webhooks-Svix-FF5A5F" />
  <img src="https://img.shields.io/badge/deps-requests-2CA5E0" />
</p>

<em>A tiny always-on web server that receives Fathom "meeting content ready" webhooks, verifies the Svix signature, and extracts the transcript — behind one permanent URL.</em>

</div>

---

## ✨ Why it exists

Fathom needs a public URL to deliver webhooks. Tunnels like `trycloudflare` hand you a **new** URL
every session, so you'd have to re-register it in Fathom each time. Deploying this on Render gives a
**permanent URL** — register it in Fathom **once** and forget it.

## 🗂️ Files

| File | Purpose |
|---|---|
| `fathom_webhook.py` | The server (binds to `$PORT`, which Render injects) |
| `fathom_api.py` | Fathom REST client — used as a fallback when a webhook omits the transcript |
| `send_test_webhook.py` | Fire a sample signed webhook at the server locally |
| `render.yaml` | Render blueprint (runtime, build & start commands) |
| `requirements.txt` | Just `requests` |

## 🚀 Deploy (one time)

1. Push this folder to its **own** GitHub repo (private is fine).
2. **Render → New → Web Service** → connect that repo. Render reads `render.yaml`
   (runtime: python · build `pip install -r requirements.txt` · start `python fathom_webhook.py`).
3. In the service's **Environment**, add:
   - `FATHOM_WEBHOOK_SECRET` = the `whsec_…` secret Fathom shows for this webhook
   - `FATHOM_API_KEY` = your Fathom API key
4. Deploy. Render gives a URL like `https://fathom-webhook.onrender.com`.
5. In **Fathom → Settings → Webhooks**, set the URL to `https://<your-service>.onrender.com/fathom`
   and enable **"include transcript."** Visiting the base URL should print `fathom_webhook is up.`

## ⚠️ Free-tier limits (read before relying on it)

- **Ephemeral disk** — transcripts written to `transcripts/` are **lost on restart/redeploy**. To
  keep them durably, the webhook should forward each one (e.g. create a task / POST to an ops
  console) — that's the next iteration.
- **Sleeps after ~15 min idle** — the first webhook after idle triggers a cold start (~30–60 s).
  Fathom retries failed deliveries, so it still lands, just not instantly.
- For always-instant + durable, use a paid Render disk, or a named Cloudflare tunnel to an
  always-on box (fixed hostname, transcripts persist locally).

## 🔒 No secrets in this repo

Both scripts read `FATHOM_WEBHOOK_SECRET` / `FATHOM_API_KEY` from the environment only — never
hardcoded. `.env` is gitignored.

---

<div align="center">
<sub>Built by <a href="https://github.com/hirakparekh">@hirakparekh</a></sub>
</div>
