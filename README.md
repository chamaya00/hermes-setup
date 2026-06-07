# hermes-setup

Deployment config for a self-hosted [Hermes](https://github.com/NousResearch/hermes-agent)
agent. The gateway runs as a systemd service on a DigitalOcean Droplet; CI
(`.github/workflows/deploy.yml`) ships `config.yaml` and injects secrets on every
push to `main`. A daily workflow (`update-free-model.yml`) keeps the model pointed
at a live free OpenRouter slug.

## Integrated providers

| Provider | Role | Secret(s) | Cost model |
|---|---|---|---|
| **OpenRouter** | LLM inference (`model.provider`) | `OPENROUTER_API_KEY` | Pay-per-token — currently pinned to `:free` models only |
| **Modal** | Serverless terminal sandbox (`terminal.backend`) | `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` | Pay-per-compute-second (free monthly allowance) |
| **DigitalOcean** | Droplet that hosts the gateway | `DROPLET_IP` / `DROPLET_SSH_KEY` | Flat monthly Droplet fee |
| **Discord** | Chat gateway | `DISCORD_BOT_TOKEN` / `DISCORD_ALLOWED_USERS` | Free |
| **Firecrawl** | Web access — `web_search` / `web_extract` | `FIRECRAWL_KEY` (optional) | Free tier: 500 credits/mo |

### Web access

The agent reads and searches the web through Hermes's lightweight
`web_search` / `web_extract` tools, which run server-side via hosted **Firecrawl**
— no local browser. Hermes auto-enables them the moment `FIRECRAWL_API_KEY` is in
`~/.hermes/.env`; the deploy injects it from the optional **`FIRECRAWL_KEY`**
GitHub secret (unset → web tools stay off, deploy still succeeds). Get a free key
(500 credits/mo) at <https://www.firecrawl.dev>, or self-host **SearXNG** for a
no-account, no-key alternative.

We deliberately do **not** run the local Playwright/Chromium *browser tool*: it
exists for *interacting* with pages (clicking, forms, JS-only SPAs), and on this
root/tiny-droplet box it was a recurring deploy-failure source (Chromium won't
launch as root without `--no-sandbox`; the from-scratch `npm install` OOM-killed
the box). The installer therefore runs with `--skip-browser`. Re-introduce the
browser tool only if the agent genuinely needs page interaction, not just reading
or search.

Secrets are **not** in `config.yaml`. They live as GitHub Actions secrets and are
written to `~/.hermes/.env` on the Droplet by the deploy workflow.

## Cost & spending limits

Hermes has **no billing view or spend cap of its own** — each provider's own
console is the source of truth, and the only place a real spending limit can be
enforced. Because the model picker only ever selects `:free` OpenRouter slugs,
day-to-day token spend should be ~$0; the point of the limits below is to **cap
the blast radius if a key ever leaks** and gets used on paid models or compute.

### Checking what you're spending

Run the bundled snapshot — it pulls OpenRouter usage via the API and links the
two providers that have no scriptable billing endpoint:

```bash
scripts/check_costs.sh
```

It reads `OPENROUTER_API_KEY` from the environment, or falls back to
`~/.hermes/.env` (override with `HERMES_ENV_FILE=/path/to/.env`). It is read-only.

Per-provider consoles:

- **OpenRouter** — per-request log at <https://openrouter.ai/activity>; balance at
  <https://openrouter.ai/credits>.
- **Modal** — current-cycle compute spend at <https://modal.com/settings/usage>.
- **DigitalOcean** — month-to-date at <https://cloud.digitalocean.com/account/billing>.
- **Firecrawl** — credit usage at <https://www.firecrawl.dev/app/usage> (free tier: 500/mo).

### Setting spending limits (fraud protection)

Set these **at the provider** — not in Hermes:

1. **OpenRouter (strongest lever).** Keep credits **prepaid** and turn **off
   auto-top-up** at <https://openrouter.ai/credits>, so max loss = current
   balance. Then give the Hermes key a hard **credit limit** at
   <https://openrouter.ai/settings/keys> — ideally a dedicated low-limit key you
   can revoke on its own if the Droplet is ever compromised.
2. **Modal.** Set a **workspace spending limit** (Settings → Usage / Billing);
   Modal pauses the workspace when the cap is hit.
3. **DigitalOcean.** No hard cap exists, but set a **billing alert**
   (Billing → Billing Alerts). Exposure is naturally bounded to the Droplet size.

### Key hygiene

If a key leaks, **rotate it at the provider** and update the corresponding GitHub
Actions secret. The per-key limits above only cap damage until you do.

## Layout

- `config.yaml` — non-secret Hermes behavior, shipped to the Droplet on deploy.
- `scripts/pick_free_model.py` — selects a live free, tool-capable OpenRouter model.
- `scripts/check_costs.sh` — read-only cost snapshot across providers.
- `.github/workflows/deploy.yml` — install/update Hermes (`--skip-browser`), push
  config, inject secrets (incl. the optional Firecrawl web-search key), start gateway.
- `.github/workflows/update-free-model.yml` — daily + on-demand free-model refresh.
- `docs/plans/` — design docs / handoff plans.
