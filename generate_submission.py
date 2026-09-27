#!/usr/bin/env python3
"""
generate_submission.py — produce submission.jsonl (30 lines) by running
composer.compose() directly over dataset/test_pairs.json.

This does NOT require the HTTP server to be running; it exercises the same
composer.py the bot uses at /v1/tick, so scores are consistent with what the
live judge harness will see.

Usage:
    python generate_submission.py
"""

import json
from pathlib import Path

import composer

DATASET_DIR = Path(__file__).parent / "dataset"


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    test_pairs = load_json(DATASET_DIR / "test_pairs.json")["pairs"]

    categories = {}
    for f in (DATASET_DIR / "categories").glob("*.json"):
        c = load_json(f)
        categories[c["slug"]] = c

    out_lines = []
    sent_bodies = set()

    for pair in test_pairs:
        test_id = pair["test_id"]
        trigger_id = pair["trigger_id"]
        merchant_id = pair["merchant_id"]
        customer_id = pair.get("customer_id")

        trigger = load_json(DATASET_DIR / "triggers" / f"{trigger_id}.json")
        merchant = load_json(DATASET_DIR / "merchants" / f"{merchant_id}.json")
        category = categories[merchant["category_slug"]]
        customer = load_json(DATASET_DIR / "customers" / f"{customer_id}.json") if customer_id else None

        composed = composer.compose(category, merchant, trigger, customer, sent_bodies=sent_bodies)
        sent_bodies.add(composed["body"])

        out_lines.append({
            "test_id": test_id,
            "body": composed["body"],
            "cta": composed["cta"],
            "send_as": composed["send_as"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })

    out_path = Path(__file__).parent / "submission.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for line in out_lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    print(f"Wrote {len(out_lines)} lines to {out_path}")


if __name__ == "__main__":
    main()
