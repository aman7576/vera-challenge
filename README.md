# Vera Challenge Submission

## Approach

A **deterministic, template-driven composer** rather than a raw LLM-prompt
composer. `composer.py` has one dispatch function per `trigger.kind` (19
merchant-facing kinds + 7 customer-facing kinds, covering every kind in
`triggers_seed.json` and `generate_dataset.py`'s expansion list). Each
handler:

- pulls the actual numbers/dates/quotes/citations straight out of the
  trigger payload and category digest — never invents anything;
- reads `merchant.signals`, `merchant.customer_aggregate`, and
  `merchant.offers` to personalize (e.g. comparing a competitor's offer
  against *this merchant's own* active offer, not a generic one);
- picks Hindi-English code-mix vs. English based on
  `merchant.identity.languages` / `customer.identity.language_pref`;
- is tagged in its `rationale` with which compulsion lever(s) it's using
  (specificity, loss aversion, social proof, effort externalization,
  curiosity, reciprocity, "ask the merchant", single-binary CTA).

**Why not an LLM prompt as the primary path?** Reliability. The judge times
out `/v1/tick` and `/v1/reply` at 30s, penalizes timeouts, and re-runs the
whole 60-minute test with a 10 req/s cap. A template composer is
sub-millisecond, needs no API key, never rate-limits, and is trivially
deterministic — which matters because the brief requires determinism given
identical inputs. An optional `VERA_LLM_POLISH=1` + `ANTHROPIC_API_KEY` path
exists (`composer._llm_polish`) that runs a temperature=0 Claude rewrite
pass *constrained to rephrasing only* (system prompt explicitly forbids
adding any fact not already in the draft) — off by default so the bot's
behavior doesn't depend on network/API availability.

Multi-turn handling (`conversation_handlers.py`) covers the three Phase-4
replay scenarios directly:

- **Auto-reply hell** — canned-text pattern match + verbatim-repeat counter;
  tries once more on first detection, then exits (`action: end`) on the
  second occurrence.
- **Intent transition** — a small go-ahead lexicon (`yes`, `let's do it`,
  `ok karo`, `sign me up`, ...) routes straight to `action`, never back to a
  qualifying question — this is the exact anti-pattern (Pattern D) called
  out in the brief.
- **Hostile / off-topic** — de-escalates without engaging, offers a graceful
  off-ramp, and redirects off-topic asks (GST, insurance, etc.) back to
  scope without ignoring them.

`respond()` also implements `wait` (recipient asked for time) and hard
`end` (explicit not-interested/STOP) so all three `/v1/reply` action types
are exercised.

## What's in this repo

```
bot.py                     FastAPI server — the 5 required endpoints
composer.py                compose(category, merchant, trigger, customer=None) -> dict
conversation_handlers.py   respond(state, message) -> {action: send|wait|end}
generate_submission.py     offline script: runs composer.py over test_pairs.json -> submission.jsonl
generate_dataset.py        (provided) expands seeds/ -> dataset/
judge_simulator.py         (provided) local test harness
dataset/                   full expanded dataset (5 categories, 50 merchants, 200 customers, 100 triggers, test_pairs.json)
seeds/                     original seed files, kept for reproducibility
submission.jsonl           30 lines, one per canonical test pair
requirements.txt
```

## Run it

```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Local self-test (edit the config block at the top of `judge_simulator.py`
first — bot URL + LLM provider/key for the *judge's* scoring calls; the bot
itself needs no key):

```bash
export BOT_URL=http://localhost:8080
python judge_simulator.py
```

Regenerate `submission.jsonl` (no server needed — calls `composer.compose`
directly):

```bash
python generate_submission.py
```

## Tradeoffs

- **Template over pure-LLM**: gains reliability/determinism/speed, costs
  some phrasing variety on trigger kinds outside the 25 hand-authored seed
  triggers (the generator's ~75 auto-expanded triggers carry placeholder
  payloads with no real fields — these correctly fall back to a
  signal-based generic message rather than fabricating numbers, but read
  more generically than the hand-crafted branches).
- **No persistent trigger-payload memory across `/v1/reply` turns**: the
  conversation state remembers turns and repetition, but `respond()`
  doesn't re-derive the original trigger's specific facts (e.g. re-citing
  the JIDA number three turns later) — a fuller build would carry the
  originating `trigger_id`'s payload through the conversation state and
  thread it into every follow-up, not just the opening message.
- **Auto-reply lexicon is hand-written**, not learned — works for the
  patterns shown in the brief and testing brief; a larger deployment would
  want it fed from real WA Business canned-reply text.

## What additional context would have helped most

1. **Real open-slot inventory** for `recall_due`/`appointment_tomorrow` on
   merchants beyond the seed set (the generated merchants have no booking
   calendar at all).
2. **A resolved digest-item link on every `research_digest`/`regulation_change`
   trigger for generated (non-seed) merchants** — right now only the 5
   category files' digest items exist, so a merchant in a category whose
   digest item ID doesn't match any seed trigger degrades to a generic line.
3. **Explicit merchant `owner_first_name` hygiene** — a couple of generated
   dentist records store `"Dr. Asha"` as `owner_first_name` (title baked
   in), which the composer has to detect and strip to avoid "Dr. Dr. Asha".
