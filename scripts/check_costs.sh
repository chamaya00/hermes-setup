#!/usr/bin/env bash
#
# check_costs.sh — one-command snapshot of what this Hermes deployment is costing.
#
# Why this exists
# ---------------
# Hermes talks to several paid providers (OpenRouter for inference, Modal for the
# sandbox, DigitalOcean for the host) but has no built-in billing view of its own.
# This script pulls the one number you can read programmatically — your OpenRouter
# token spend, the only provider with a clean usage API — and prints direct links
# for the two that don't (Modal, DigitalOcean). It is read-only: it never spends,
# changes a limit, or mutates anything.
#
# Key resolution order for OPENROUTER_API_KEY:
#   1. the OPENROUTER_API_KEY environment variable, if already exported;
#   2. otherwise it is read from ~/.hermes/.env (where deploy.yml injects it).
# Override the env file with HERMES_ENV_FILE=/path/to/.env if yours lives elsewhere.
#
# Usage:
#   scripts/check_costs.sh          # print the cost snapshot
#   scripts/check_costs.sh --help   # show this help
#
# Requires: curl and python3 (both already present on the Droplet and in CI).

set -euo pipefail

ENV_FILE="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"
OPENROUTER_BASE="https://openrouter.ai/api/v1"

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

[ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ] && usage

# --- Resolve the OpenRouter key ------------------------------------------------
# Prefer an already-exported env var; fall back to the deployed .env. We grep a
# single line rather than sourcing the file so we don't accidentally execute or
# import unrelated secrets into this shell.
if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -f "$ENV_FILE" ]; then
  OPENROUTER_API_KEY="$(
    grep -E '^[[:space:]]*OPENROUTER_API_KEY[[:space:]]*=' "$ENV_FILE" \
      | tail -n1 \
      | cut -d= -f2- \
      | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/^"//; s/"$//; s/^'\''//; s/'\''$//'
  )" || true
fi

echo "=============================================="
echo " Hermes cost snapshot — $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "=============================================="
echo

# --- OpenRouter: the one provider with a usable usage API ----------------------
echo "## OpenRouter (LLM inference)"
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  echo "  ! No OPENROUTER_API_KEY found in the environment or $ENV_FILE."
  echo "    Export it, or pass HERMES_ENV_FILE=/path/to/.env, then re-run."
else
  # Two endpoints: /credits is the account-wide balance vs. lifetime usage;
  # /key is THIS key's own usage and (if you set one) its hard credit limit.
  credits_json="$(curl -fsS "$OPENROUTER_BASE/credits" \
    -H "Authorization: Bearer $OPENROUTER_API_KEY" 2>/dev/null || echo '')"
  key_json="$(curl -fsS "$OPENROUTER_BASE/key" \
    -H "Authorization: Bearer $OPENROUTER_API_KEY" 2>/dev/null || echo '')"

  if [ -z "$credits_json" ] && [ -z "$key_json" ]; then
    echo "  ! Could not reach OpenRouter (network error or invalid key)."
  else
    CREDITS_JSON="$credits_json" KEY_JSON="$key_json" python3 - <<'PY'
import json, os

def load(name):
    raw = os.environ.get(name, "")
    try:
        return (json.loads(raw) or {}).get("data") or {}
    except Exception:
        return {}

def money(v):
    return f"${float(v):,.4f}" if v is not None else "n/a"

credits = load("CREDITS_JSON")
key = load("KEY_JSON")

total = credits.get("total_credits")
used = credits.get("total_usage")
if total is not None and used is not None:
    remaining = float(total) - float(used)
    print(f"  Account balance loaded : {money(total)}")
    print(f"  Account lifetime spend : {money(used)}")
    print(f"  Remaining credit       : {money(remaining)}")

usage = key.get("usage")
limit = key.get("limit")
limit_remaining = key.get("limit_remaining")
is_free = key.get("is_free_tier")
print(f"  This key's spend       : {money(usage)}")
if limit is None:
    print("  This key's hard limit  : NONE  <-- set one at openrouter.ai/settings/keys")
else:
    print(f"  This key's hard limit  : {money(limit)} (remaining {money(limit_remaining)})")
if is_free is not None:
    print(f"  Free-tier key          : {is_free}")
PY
  fi
fi
echo

# --- Modal & DigitalOcean: no clean usage API; link to the consoles ------------
echo "## Modal (serverless sandbox)"
echo "  No scriptable billing API. Check current-cycle compute spend at:"
echo "    https://modal.com/settings/usage"
echo "  Set a workspace spending limit at:"
echo "    https://modal.com/settings  ->  Usage / Billing"
echo

echo "## DigitalOcean (Droplet host)"
echo "  Flat monthly Droplet fee. Month-to-date and billing alerts at:"
echo "    https://cloud.digitalocean.com/account/billing"
echo

echo "----------------------------------------------"
echo "Reminder: the durable fraud protection is per-provider spending"
echo "limits, not Hermes. See README.md > 'Cost & spending limits'."
