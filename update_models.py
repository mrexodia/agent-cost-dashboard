#!/usr/bin/env python3
"""Refresh models.json with the latest model pricing from OpenRouter.

Run this whenever you want to update pricing:

    python3 update_models.py

The dashboard reads the committed models.json at runtime and never hits the
network itself, so pricing updates are an explicit, reviewable change.
"""

import json
import sys
import urllib.request
from pathlib import Path

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
MODELS_FILE = Path(__file__).parent / "models.json"


def main() -> int:
    print(f"Fetching {OPENROUTER_MODELS_URL} ...")
    try:
        req = urllib.request.Request(
            OPENROUTER_MODELS_URL, headers={"User-Agent": "cost-dashboard"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
    except Exception as e:
        print(f"Failed to fetch models: {e}", file=sys.stderr)
        return 1

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        print(f"Response was not valid JSON: {e}", file=sys.stderr)
        return 1

    models = data.get("data", [])
    if not models:
        print("Response contained no models; refusing to overwrite.", file=sys.stderr)
        return 1

    MODELS_FILE.write_bytes(payload)
    print(f"Wrote {len(models)} models to {MODELS_FILE} ({len(payload):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
