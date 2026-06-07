# Plan: Auto-trigger the free-model updater on a 404 (self-healing recovery)

**Status:** Not started — handoff plan for a future implementer.
**Owner:** (assign on pickup)
**Related:** `scripts/pick_free_model.py`, `.github/workflows/update-free-model.yml`, `.github/workflows/deploy.yml`

---

## 1. Goal

Close the recovery gap left by the scheduled updater. Today the daily cron +
manual `workflow_dispatch` keep `config.yaml` pointed at a live free model, but
if a free endpoint dies *between* runs, Hermes emits
`API call failed after 3 retries: HTTP 404: No endpoints found` until someone
re-runs the workflow by hand. This plan makes that recovery **automatic**: when
Hermes hits that 404, it (or something watching it) triggers
`update-free-model.yml`, which reselects a live model, commits, and redeploys.

**Definition of done:** Kill the currently-selected free endpoint (or simulate
the 404) and, with no human action, the system reselects a working model and the
gateway answers again within a few minutes.

---

## 2. Background / current state

- The gateway runs on the Droplet as a **systemd service** installed by
  `deploy.yml` via `hermes gateway install --system --run-as-user root`. Its
  logs therefore go to **journald** (reachable with `journalctl -u <unit>`).
- `deploy.yml` ships `config.yaml` to the Droplet (`scp ... ~/.hermes/config.yaml`)
  and injects secrets with `hermes config set ...`. It is the single deploy path.
- `update-free-model.yml` already exposes a `workflow_dispatch` trigger and is
  idempotent: it commits `config.yaml` only when the slug actually changes, then
  dispatches `deploy.yml`. **No change to the picker is required** — we only need
  something to *call* the dispatch on a 404.
- The Droplet does **not** have the repo checked out (deploy only scp's
  `config.yaml`), so the picker script is not available locally. Recovery must go
  through the GitHub workflow, not a local re-pick (see §6 for the local
  fast-path as an optional enhancement).

---

## 3. Decision to make first (investigation step)

Before building, spend ~30 min determining how Hermes can react to an error,
because it picks the approach:

- **Q1.** Does Hermes (NousResearch/hermes-agent) expose any hook / plugin /
  `on_error` / notification mechanism that runs a command or webhook when a model
  call fails? Check `hermes --help`, `hermes config --help`, the docs, and the
  installed source under `/usr/local/lib/hermes-agent/`.
- **Q2.** What is the exact systemd **unit name**? Run
  `systemctl list-units --type=service | grep -i hermes` on the Droplet
  (likely `hermes-gateway.service`, but confirm).
- **Q3.** What is the exact log line on this failure? Confirm the literal text
  (`No endpoints found` and/or `HTTP 404`) via `journalctl -u <unit>`.

**Recommendation:** Even if Q1 finds a native hook, the **log-watcher sidecar
(Approach A)** is the robust default — it needs zero cooperation from Hermes
internals and survives Hermes version drift (the README already warns the schema
drifts between releases). Use Approach B only if Q1 reveals a clean, supported
hook.

---

## 4. Approach A — log-watcher sidecar (recommended)

A tiny root service on the Droplet tails the gateway log, matches the 404
signature, debounces, and POSTs a `workflow_dispatch` to GitHub.

### 4.1 Components to build

1. **`scripts/model-404-watcher.sh`** (new)
   - Tail logs live: `journalctl -u "$HERMES_UNIT" -f -n0 -o cat`.
   - Match the failure signature (anchor on the specific phrase to avoid false
     positives): `No endpoints found` (optionally also `HTTP 404` near it).
   - **Debounce:** keep a timestamp file (e.g. `/run/hermes-model-watcher.last`);
     ignore matches within a cooldown window (default **15 min**) so a burst of
     failures fires at most one dispatch.
   - **Trigger:** POST to the Actions REST API:
     ```
     curl -fsS -X POST \
       -H "Authorization: Bearer $MODEL_UPDATER_PAT" \
       -H "Accept: application/vnd.github+json" \
       https://api.github.com/repos/chamaya00/hermes-setup/actions/workflows/update-free-model.yml/dispatches \
       -d '{"ref":"main"}'
     ```
   - Log every match + dispatch (and every debounced skip) to journald so the
     behavior is auditable.
   - Read config from an env file (unit, repo, cooldown, PAT) — see 4.3.

2. **`deploy/hermes-model-watcher.service`** (new systemd unit)
   - `Restart=always`, `After=network-online.target`, runs the watcher script.
   - `EnvironmentFile=/etc/hermes-model-watcher.env` for the PAT + settings.

3. **`deploy.yml` additions** (new steps, mirroring existing patterns)
   - `scp` the watcher script + unit file to the Droplet.
   - Write `/etc/hermes-model-watcher.env` (chmod 600) with
     `MODEL_UPDATER_PAT`, `HERMES_UNIT`, `GITHUB_REPO`, `COOLDOWN_SECONDS`.
   - `systemctl daemon-reload && systemctl enable --now hermes-model-watcher`.
   - End on a status check (mirror the gateway step's loud-failure pattern).

### 4.2 Secret / permission needs

- New repo secret **`MODEL_UPDATER_PAT`**: a *fine-grained* PAT scoped to
  `chamaya00/hermes-setup` only, with **Actions: read & write** permission
  (enough to POST a workflow dispatch). Do **not** reuse a broad classic token.
- The default `GITHUB_TOKEN` cannot be used from the Droplet (it only exists
  inside Actions runs), hence the dedicated PAT.
- Store it the same way other secrets flow: as a GitHub Actions secret, written
  to the Droplet env file by `deploy.yml`. Keep it out of `~/.hermes/.env` since
  it is infra, not a Hermes setting; a root-only `/etc/hermes-model-watcher.env`
  is cleaner.

### 4.3 Safety / loop-prevention (important)

- **Cooldown** (above) caps dispatch frequency regardless of error volume.
- **Idempotent workflow:** if the picker reselects the *same* slug (nothing
  better is live), `update-free-model.yml` makes no commit and does not redeploy
  — so a flapping endpoint won't cause a redeploy storm; at worst the watcher
  just hits cooldown repeatedly and logs skips.
- **No infinite loop on total outage:** if *no* free+tools model is live, the
  picker exits non-zero, the workflow fails, no deploy happens, and the watcher
  keeps cooling down. Add an alert (see §7) so a human notices a sustained
  outage instead of it failing silently.
- Consider a **daily dispatch cap** (e.g. max 6/day) as a second guardrail.

---

## 5. Approach B — native Hermes hook (only if §3/Q1 finds one)

If Hermes supports an `on_error`/notification command or webhook:
- Point it at a one-line command that runs the same `curl` dispatch from §4.1
  (with the same PAT + cooldown logic, ideally wrapped in a small script so the
  debounce still applies).
- Pros: no log parsing, exact error semantics. Cons: couples to Hermes internals
  that may change between versions — re-verify on every `hermes update`.

Keep the dispatch script shared between A and B so only the *trigger* differs.

---

## 6. Optional enhancement — local fast-path (lower latency)

Workflow + deploy recovery takes a few minutes (the failed DM won't be answered,
but subsequent ones recover). If near-instant recovery is wanted later:
- Ship the repo (or just `pick_free_model.py`) to the Droplet in `deploy.yml`.
- On 404, run the picker locally, write `~/.hermes/config.yaml`, and
  `hermes gateway restart` — recovery in seconds.
- **Still** dispatch the workflow afterward to reconcile the repo (git history /
  single source of truth), otherwise the Droplet and repo drift.
- More moving parts; treat as a follow-up, not part of the first cut.

---

## 7. Observability

- Watcher logs matches, dispatches, and debounced skips to journald
  (`journalctl -u hermes-model-watcher`).
- On a *failed* workflow run (no live model found), surface it: simplest is to
  rely on GitHub's "workflow run failed" email; better is a Discord ping to the
  `home_chat_id` channel. Decide during implementation.

---

## 8. Task checklist (for the implementer)

- [ ] §3 investigation: confirm unit name, exact 404 log line, and whether a
      native Hermes hook exists. Pick Approach A or B.
- [ ] Create `MODEL_UPDATER_PAT` (fine-grained, this repo, Actions: read/write)
      and add it as a GitHub Actions secret.
- [ ] Write the shared dispatch+debounce script (`scripts/model-404-watcher.sh`).
- [ ] Write `deploy/hermes-model-watcher.service`.
- [ ] Extend `deploy.yml`: ship script + unit, write `/etc/hermes-model-watcher.env`
      (chmod 600), enable+start the service, status-check.
- [ ] (If Approach B) wire the Hermes hook to the dispatch script instead of/in
      addition to the watcher.
- [ ] Add the daily-cap guardrail and outage alerting (§4.3, §7).
- [ ] Test (see §9). Update `README.md` with how the auto-recovery works and the
      new secret.

---

## 9. Test plan

1. **Picker still green:** `python scripts/pick_free_model.py` selects a live
   model (unchanged behavior).
2. **Dispatch path:** manually POST the workflow dispatch with the PAT; confirm
   `update-free-model.yml` runs and (if slug changed) `deploy.yml` follows.
3. **Watcher match:** on the Droplet, inject the exact failure line into the log
   (e.g. `logger -t hermes-gateway "... No endpoints found ..."` or point the
   watcher at a test unit) and confirm exactly one dispatch fires.
4. **Debounce:** inject several matches inside the cooldown; confirm only one
   dispatch, with skips logged.
5. **End-to-end:** temporarily pin `config.yaml` to a dead `:free` slug, deploy,
   send a Discord DM to provoke the real 404, and confirm hands-off recovery:
   watcher → workflow → deploy → gateway answers again.
6. **Outage behavior:** simulate "no free model live" (e.g. empty the preference
   list / mock) and confirm the workflow fails loudly without a redeploy loop.

---

## 10. Open questions

- Exact gateway unit name and 404 log string (resolve in §3).
- Does Hermes have a usable native error hook (Approach B viability)?
- Acceptable recovery latency — is the workflow path (minutes) fine, or is the
  local fast-path (§6) wanted in v1?
- Where to alert on sustained outage (email vs Discord `home_chat_id`).
