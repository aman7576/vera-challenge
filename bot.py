"""
bot.py — HTTP server implementing the 5-endpoint judge contract from
challenge-testing-brief.md:

    POST /v1/context    receive a (scope, context_id, version) push
    POST /v1/tick        periodic wake-up; bot may initiate proactive sends
    POST /v1/reply       synchronous reply to a merchant/customer message
    GET  /v1/healthz     liveness probe
    GET  /v1/metadata    bot identity

Run:
    uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import composer
import conversation_handlers as ch

app = FastAPI(title="vera-challenge-bot")
START = time.time()

# ---------------------------------------------------------------------------
# In-memory state (fine per the brief: "storing in memory is fine, don't
# restart between calls"). Keyed exactly as the judge's reference skeleton
# implies: (scope, context_id) -> {"version": int, "payload": dict}
# ---------------------------------------------------------------------------

contexts: dict[tuple[str, str], dict] = {}
conversations: dict[str, dict] = {}   # conversation_id -> ConversationState-ish dict
sent_bodies: set[str] = set()         # global anti-repetition guard (§10 penalty)


def _get_ctx(scope: str, context_id: str) -> Optional[dict]:
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


def _merchant_and_category(merchant_id: str) -> tuple[Optional[dict], Optional[dict]]:
    merchant = _get_ctx("merchant", merchant_id)
    if not merchant:
        return None, None
    category = _get_ctx("category", merchant.get("category_slug"))
    return merchant, category


# ---------------------------------------------------------------------------
# GET /v1/healthz
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": counts,
    }


# ---------------------------------------------------------------------------
# GET /v1/metadata
# ---------------------------------------------------------------------------

@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vera Challenge",
        "team_members": [],
        "model": "template-composer (deterministic; optional Claude polish pass via ANTHROPIC_API_KEY + VERA_LLM_POLISH=1)",
        "approach": (
            "Deterministic, dispatch-per-trigger-kind template composer that reads "
            "category/merchant/trigger/customer contexts directly and anchors every "
            "message on a real number/date/quote from the payload. No LLM dependency "
            "required to run reliably within the 30s budget; optional Claude rewrite "
            "pass available for phrasing polish only (never adds facts)."
        ),
        "contact_email": "",
        "version": "1.0.0",
        "submitted_at": datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# POST /v1/context
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        raise HTTPException(status_code=400, detail={"accepted": False, "reason": "invalid_scope", "details": f"unknown scope '{body.scope}'"})

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] > body.version:
        raise HTTPException(status_code=409, detail={"accepted": False, "reason": "stale_version", "current_version": cur["version"]})
    if cur and cur["version"] == body.version:
        return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": body.delivered_at}

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# POST /v1/tick
# ---------------------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions: list[dict] = []

    for trg_id in body.available_triggers:
        if len(actions) >= 20:  # §5 action-count cap per tick
            break

        trigger = _get_ctx("trigger", trg_id)
        if not trigger:
            continue

        merchant_id = trigger.get("merchant_id")
        merchant, category = _merchant_and_category(merchant_id) if merchant_id else (None, None)
        if not (merchant and category):
            continue

        customer = None
        customer_id = trigger.get("customer_id")
        if customer_id:
            customer = _get_ctx("customer", customer_id)

        suppression_key = trigger.get("suppression_key", "")
        conv_id = f"conv_{merchant_id}_{trg_id}"

        # Suppression: don't re-fire a trigger we've already actioned, and
        # don't start a duplicate conversation for the same (merchant, trigger).
        if suppression_key and any(c.get("suppression_key") == suppression_key for c in conversations.values()):
            continue
        if conv_id in conversations:
            continue

        composed = composer.compose(category, merchant, trigger, customer, sent_bodies=sent_bodies)
        if not composed.get("body"):
            continue  # restraint: nothing worth sending

        send_as = composed["send_as"]
        conversations[conv_id] = ch.new_conversation_state(
            conversation_id=conv_id,
            merchant_id=merchant_id,
            customer_id=customer_id,
            trigger_id=trg_id,
            suppression_key=suppression_key,
            send_as=send_as,
        )
        ch.record_bot_turn(conversations[conv_id], composed["body"])
        sent_bodies.add(composed["body"])

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": send_as,
            "trigger_id": trg_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            "template_params": [merchant.get("identity", {}).get("name", ""), trigger.get("kind", "")],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": suppression_key,
            "rationale": composed["rationale"],
        })

    return {"actions": actions}


# ---------------------------------------------------------------------------
# POST /v1/reply
# ---------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    state = conversations.get(body.conversation_id)
    if state is None:
        # Judge is replaying against a conversation we don't remember (e.g.
        # a fresh replay scenario). Bootstrap minimal state so we can still
        # respond sensibly rather than erroring.
        state = ch.new_conversation_state(
            conversation_id=body.conversation_id,
            merchant_id=body.merchant_id,
            customer_id=body.customer_id,
            trigger_id=None,
            suppression_key="",
            send_as="vera" if not body.customer_id else "merchant_on_behalf",
        )
        conversations[body.conversation_id] = state

    merchant, category = (None, None)
    if state.get("merchant_id"):
        merchant, category = _merchant_and_category(state["merchant_id"])

    decision = ch.respond(state, body.message, merchant=merchant, category=category,
                           turn_number=body.turn_number, sent_bodies=sent_bodies)

    if decision["action"] == "send":
        sent_bodies.add(decision["body"])
        ch.record_bot_turn(state, decision["body"])
        return {"action": "send", "body": decision["body"], "cta": decision.get("cta", "open_ended"),
                "rationale": decision["rationale"]}
    if decision["action"] == "wait":
        return {"action": "wait", "wait_seconds": decision.get("wait_seconds", 1800),
                "rationale": decision["rationale"]}
    # "end"
    return {"action": "end", "rationale": decision["rationale"]}


# ---------------------------------------------------------------------------
# Optional teardown hook (§11 of the testing brief)
# ---------------------------------------------------------------------------

@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    sent_bodies.clear()
    return {"status": "wiped"}
