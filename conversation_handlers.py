"""
conversation_handlers.py — multi-turn conversation state machine.

Implements the "Open challenges" from challenge-brief.md §12 and the Phase-4
replay scenarios from challenge-testing-brief.md §4:

  1. Auto-reply detection: same message verbatim 3+ times -> treat as WA
     Business canned auto-reply, try once more, then exit gracefully.
  2. Intent-transition handling: explicit go-ahead phrases ("yes", "let's do
     it", "ok karo", "go ahead") -> switch straight to action, never re-ask
     a qualifying question.
  3. Hostile / off-topic handling: acknowledge briefly, redirect once, stay
     polite, don't escalate.
  4. Graceful exit: hard "not interested" / repeated silence -> action="end".
  5. `respond(state, merchant_message)` — the optional conversation_handlers
     contract from the brief (§7.4), reused internally by bot.py's
     /v1/reply so there's exactly one place this logic lives.
"""

from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Lexicons (kept small & explicit — no ML classifier needed for this scope)
# ---------------------------------------------------------------------------

AUTO_REPLY_MARKERS = [
    "thank you for contacting", "thank you for reaching out", "we will get back to you",
    "aapki jaankari ke liye", "hamari team tak pahuncha", "automated assistant",
    "this is an automated", "yeh ek automatic",
]

INTENT_GO_AHEAD = [
    r"\byes\b", r"\bya\b(?!r)", r"go ahead", r"let'?s do it", r"sure", r"okay,? do it",
    r"ok karo", r"chalo karo", r"kar do", r"start karo", r"i want to join",
    r"judrna hai", r"jodna hai", r"sign me up", r"proceed", r"confirm",
]

HARD_NEGATIVE = [
    r"\bnot interested\b", r"\bno thanks?\b", r"\bstop\b", r"nahi chahiye",
    r"don'?t contact", r"unsubscribe", r"band karo", r"leave me alone",
]

HOSTILE_MARKERS = [
    r"\bidiot\b", r"\bstupid\b", r"\bshut up\b", r"\bnonsense\b", r"bewakoof",
    r"\bfraud\b", r"\bscam\b",
]

WAIT_MARKERS = [
    r"\blater\b", r"call.*(tomorrow|later)", r"give me (a|some) time", r"busy right now",
    r"baad mein", r"abhi busy",
]

OFF_TOPIC_HINT = [
    r"\bgst\b", r"\bincome tax\b", r"\bloan\b", r"\binsurance\b", r"weather", r"cricket score",
]


def _match_any(patterns: list[str], text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in patterns)


# ---------------------------------------------------------------------------
# State factory
# ---------------------------------------------------------------------------

def new_conversation_state(conversation_id: str, merchant_id: Optional[str],
                            customer_id: Optional[str], trigger_id: Optional[str],
                            suppression_key: str, send_as: str) -> dict:
    return {
        "conversation_id": conversation_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "trigger_id": trigger_id,
        "suppression_key": suppression_key,
        "send_as": send_as,
        "turns": [],                 # [{"from": "vera"|"merchant"|"customer", "body": str}]
        "unanswered_nudges": 0,
        "last_incoming_message": None,
        "repeat_count": 0,           # consecutive identical incoming messages
        "ended": False,
    }


def record_bot_turn(state: dict, body: str) -> None:
    state["turns"].append({"from": state.get("send_as", "vera"), "body": body})
    state["unanswered_nudges"] += 1


# ---------------------------------------------------------------------------
# Core reply logic
# ---------------------------------------------------------------------------

def respond(state: dict, merchant_message: str, merchant: Optional[dict] = None,
            category: Optional[dict] = None, turn_number: int = 0,
            sent_bodies: Optional[set] = None) -> dict:
    """
    Returns {"action": "send"|"wait"|"end", "body"?, "cta"?, "rationale": str,
    "wait_seconds"?}.
    """
    state["turns"].append({"from": "incoming", "body": merchant_message})
    state["unanswered_nudges"] = 0

    if state.get("ended"):
        return {"action": "end", "rationale": "This conversation has already ended; keeping the exit final."}

    # --- repetition tracking for auto-reply detection ---
    if merchant_message == state.get("last_incoming_message"):
        state["repeat_count"] += 1
    else:
        state["repeat_count"] = 1
        state["last_incoming_message"] = merchant_message

    is_auto_reply_text = _match_any(AUTO_REPLY_MARKERS, merchant_message)

    # 1) Auto-reply hell: same verbatim text 3+ times, or clearly canned text
    #    seen at least twice -> exit gracefully. Brief: "same message
    #    verbatim 3+ times = auto-reply"; testing-brief replay sends it 4x.
    if state["repeat_count"] >= 3 or (is_auto_reply_text and state["repeat_count"] >= 2):
        state["ended"] = True
        return {
            "action": "end",
            "rationale": (
                f"Detected likely WhatsApp Business auto-reply (message repeated "
                f"{state['repeat_count']}x verbatim / matches canned-reply pattern). "
                "Exiting gracefully rather than burning further turns."
            ),
        }

    # First time we see an auto-reply-shaped message: try exactly once more
    # before giving up (matches Pattern B in the brief).
    if is_auto_reply_text and state["repeat_count"] < 2:
        return {
            "action": "send",
            "body": (
                "Samajh gayi. Team tak pahunchane se pehle, 2 minute ka kaam hai — "
                "chahein to khud ek baar dekh lein? Nahi to main directly follow up kar lungi."
            ),
            "cta": "yes_no",
            "rationale": "First occurrence of an auto-reply-shaped message; trying once more with a low-friction ask before exiting, per Pattern B.",
        }

    # 2) Hard negative -> end immediately, politely.
    if _match_any(HARD_NEGATIVE, merchant_message):
        state["ended"] = True
        return {
            "action": "end",
            "rationale": "Merchant/customer signaled explicit not-interested / stop; ending the conversation immediately and respectfully.",
        }

    # 3) Explicit "wait" signal -> back off.
    if _match_any(WAIT_MARKERS, merchant_message):
        return {
            "action": "wait",
            "wait_seconds": 1800,
            "rationale": "Recipient asked for time; backing off 30 minutes instead of pushing.",
        }

    # 4) Hostile language -> stay polite, don't escalate, redirect once.
    if _match_any(HOSTILE_MARKERS, merchant_message):
        body = "Sorry to have bothered you — happy to step back. If it's helpful later, just say the word."
        if _match_any(OFF_TOPIC_HINT, merchant_message):
            body = (
                "No worries at all. That one's outside what I can help with directly, but happy to keep "
                "helping with your listing/marketing whenever you're ready."
            )
        return {
            "action": "send", "body": body, "cta": "none",
            "rationale": "Hostile tone detected; de-escalated politely without engaging the insult, offered a graceful off-ramp.",
        }

    # 5) Off-topic (but not hostile) question -> answer briefly that it's out
    #    of scope, then steer back to the mission (per Phase-4 hostile/off-topic scenario).
    if _match_any(OFF_TOPIC_HINT, merchant_message) and not _match_any(INTENT_GO_AHEAD, merchant_message):
        return {
            "action": "send",
            "body": "That's outside what I handle (I'm focused on your listing, offers, and customer outreach) — but happy to help with anything on that front.",
            "cta": "open_ended",
            "rationale": "Off-topic ask; stayed on-mission politely instead of attempting an out-of-scope task or ignoring the merchant.",
        }

    # 6) Explicit go-ahead / intent transition -> jump straight to action,
    #    never re-qualify (this is the Pattern D anti-pattern to avoid).
    if _match_any(INTENT_GO_AHEAD, merchant_message):
        return {
            "action": "send",
            "body": "Great — starting that now. I'll have it ready shortly and will confirm here once it's done.",
            "cta": "none",
            "rationale": "Detected explicit go-ahead intent; routed straight into action mode instead of asking another qualifying question (avoids the Pattern D anti-pattern).",
        }

    # 7) Question or engaged reply -> generic helpful continuation.
    #    (In a full build this branches per trigger_id/topic; kept general
    #    here since the incoming trigger context may not be resent on /v1/reply.)
    body = "Got it — noted. Want me to go ahead and take care of that, or is there something specific you'd like changed first?"
    if sent_bodies and body in sent_bodies:
        body = "Following up — want me to proceed, or would you like a different option?"
    return {
        "action": "send",
        "body": body,
        "cta": "open_ended",
        "rationale": "Engaged reply that isn't a clear yes/no/end signal; acknowledged and offered a concrete low-friction next step.",
    }
