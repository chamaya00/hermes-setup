#!/usr/bin/env python3
"""Pick a currently-live free OpenRouter model and write it into config.yaml.

Why this exists
---------------
Hardcoding a single free slug (e.g. ``deepseek/deepseek-chat-v3-0324:free``)
breaks the moment OpenRouter rotates that free endpoint away — the API then
returns ``HTTP 404: No endpoints found`` and Hermes can't answer. OpenRouter's
free endpoints are donated, unstable capacity, so the only durable fix is to
re-check what is actually live and reselect on a schedule (and on demand).

What it does
------------
1. Fetch the public model catalogue from ``/api/v1/models``.
2. Keep only models that are BOTH free (zero prompt + completion price) AND
   advertise tool calling (``"tools"`` in ``supported_parameters``) — Hermes is
   an agent, so a model without working tool calls is useless here.
3. Pick the first slug from a curated, instruct/agent-tuned PREFERENCE list that
   is currently live. Curated-first keeps selection deterministic and avoids
   reasoning models that emit raw ``<think>`` blocks and confuse ChatML parsing.
4. If none of the preferred slugs are live, fall back to any free+tool-capable
   model, skipping obvious reasoning slugs.
5. Rewrite the ``model:`` line in config.yaml. Exit 0 whether or not it changed;
   the workflow decides whether to commit based on the git diff.

Stdlib only — no pip install needed in CI.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import urllib.request

MODELS_URL = "https://openrouter.ai/api/v1/models"
CONFIG_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "config.yaml")

# Curated, instruct/agent-tuned slugs in priority order. These have solid native
# tool calling and clean ChatML behavior, so they're safe for a long agent loop.
# Sourced from current free-model research; reorder to change preference.
PREFERRED = [
    "openai/gpt-oss-20b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-coder:free",
    "openai/gpt-oss-120b:free",
]

# Heuristic markers for reasoning models that tend to emit raw <think> blocks.
# Only used for the last-resort fallback, never for the curated picks.
_REASONING_MARKERS = ("r1", "qwq", "thinking", "-think", "reasoning", ":thinking")


def _fetch_models() -> list[dict]:
    headers = {"User-Agent": "hermes-free-model-updater"}
    # The catalogue is public, but send the key if we have it (harmless).
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(MODELS_URL, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    models = payload.get("data", [])
    if not models:
        raise SystemExit("OpenRouter returned an empty model list")
    return models


def _is_free(model: dict) -> bool:
    pricing = model.get("pricing") or {}
    try:
        prompt = float(pricing.get("prompt", "0"))
        completion = float(pricing.get("completion", "0"))
    except (TypeError, ValueError):
        return False
    return prompt == 0.0 and completion == 0.0


def _supports_tools(model: dict) -> bool:
    return "tools" in (model.get("supported_parameters") or [])


def _looks_like_reasoning(slug: str) -> bool:
    s = slug.lower()
    return any(marker in s for marker in _REASONING_MARKERS)


def pick_model(models: list[dict]) -> str:
    free_tool_slugs = {
        m["id"] for m in models if _is_free(m) and _supports_tools(m)
    }
    if not free_tool_slugs:
        raise SystemExit(
            "No free models with tool-calling support are currently available"
        )

    # 1. Curated preference list — deterministic, <think>-safe.
    for slug in PREFERRED:
        if slug in free_tool_slugs:
            return slug

    # 2. Fallback: any free+tool model that isn't an obvious reasoning model,
    #    sorted for stable, reproducible selection.
    fallback = sorted(s for s in free_tool_slugs if not _looks_like_reasoning(s))
    if fallback:
        print(
            "WARNING: none of the preferred slugs are live; "
            f"falling back to {fallback[0]}",
            file=sys.stderr,
        )
        return fallback[0]

    # 3. Last resort: a reasoning model is better than no model at all.
    chosen = sorted(free_tool_slugs)[0]
    print(
        f"WARNING: only reasoning-style free models are live; using {chosen}. "
        "Expect possible <think> output.",
        file=sys.stderr,
    )
    return chosen


def write_config(slug: str) -> bool:
    """Rewrite the indented ``model:`` value in config.yaml.

    Returns True if the file content changed.
    """
    path = os.path.abspath(CONFIG_PATH)
    with open(path, "r", encoding="utf-8") as fh:
        original = fh.read()

    today = _dt.date.today().isoformat()
    comment = (
        f"# auto-selected by update-free-model workflow ({today}); "
        "live free model w/ tool calling. Do not hand-edit."
    )
    # Match only the indented inner "  model:" line (not the top-level "model:"
    # mapping key, which has no leading whitespace).
    pattern = re.compile(r"^(?P<indent>[ \t]+)model:.*$", re.MULTILINE)
    if not pattern.search(original):
        raise SystemExit("Could not find an indented 'model:' line in config.yaml")

    def _replace(match: re.Match) -> str:
        return f"{match.group('indent')}model: {slug}   {comment}"

    updated = pattern.sub(_replace, original, count=1)
    if updated == original:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(updated)
    return True


def main() -> int:
    models = _fetch_models()
    slug = pick_model(models)
    changed = write_config(slug)
    status = "updated" if changed else "unchanged"
    print(f"Selected free model: {slug} (config.yaml {status})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
