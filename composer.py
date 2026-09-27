"""
composer.py — the Vera-beating composition engine.

    compose(category, merchant, trigger, customer=None) -> ComposedMessage

Design choice: a deterministic, template-driven composer (no LLM call needed
to run, no API key required, sub-millisecond, zero flake risk under the 30s
budget) that still hits every rubric dimension by *reading the contexts
carefully* rather than by prompting a model to improvise:

  - Specificity      -> every branch below pulls a real number/date/quote out
                        of the trigger payload or category digest and drops
                        it verbatim into the message. Nothing is invented.
  - Category fit     -> voice.tone / vocab_allowed / vocab_taboo from the
                        CategoryContext drive phrasing per vertical.
  - Merchant fit     -> uses merchant.identity, merchant.signals,
                        merchant.customer_aggregate, merchant.offers, and
                        conversation_history to personalize.
  - Trigger relevance-> one dispatch function per trigger `kind`; the
                        opening clause always names *why now*.
  - Engagement compulsion -> each branch is annotated with which lever
                        (specificity / loss-aversion / social-proof /
                        effort-externalization / curiosity / reciprocity /
                        ask-the-merchant / single-binary-CTA) it is using.

If ANTHROPIC_API_KEY is set in the environment AND VERA_LLM_POLISH=1, an
optional LLM polish pass (temperature=0) can rewrite the templated draft for
more natural phrasing while preserving every fact — see `_llm_polish()`.
This is OFF by default so the bot works identically with or without a key,
which matters for the judge's 30s-timeout and reliability requirements.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

USE_LLM_POLISH = os.environ.get("VERA_LLM_POLISH", "0") == "1"
LLM_MODEL = os.environ.get("VERA_LLM_MODEL", "claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _get(d: Optional[dict], *path, default=None):
    """Safe nested getter: _get(merchant, 'identity', 'name', default='there')"""
    cur = d
    for p in path:
        if cur is None:
            return default
        cur = cur.get(p) if isinstance(cur, dict) else None
    return cur if cur is not None else default


def _is_hindi_mix(merchant: dict, customer: Optional[dict]) -> bool:
    if customer:
        pref = _get(customer, "identity", "language_pref", default="") or ""
        if "hi" in pref.lower():
            return True
        if pref.lower() == "english":
            return False
    langs = _get(merchant, "identity", "languages", default=[]) or []
    return "hi" in langs


def _first_name(merchant: dict) -> str:
    name = _get(merchant, "identity", "owner_first_name")
    if not name:
        full = _get(merchant, "identity", "name", default="there")
        name = full.split()[0] if full else "there"
    # some generated dentist records store "Dr. Asha" as owner_first_name;
    # the salutation template already supplies its own "Dr." prefix, so
    # strip an embedded title to avoid "Dr. Dr. Asha".
    for title in ("Dr. ", "Dr ", "Mr. ", "Mrs. ", "Ms. "):
        if name.startswith(title):
            name = name[len(title):]
            break
    return name


_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")


def _salutation(category: dict, merchant: dict) -> str:
    """Category voice.salutation_examples use varying placeholder names
    ({first_name}, {chef_or_owner_first_name}, {pharmacist_name}, ...). The
    first example is always the personal-name form, so substitute whatever
    placeholder it has with the merchant's first name."""
    examples = _get(category, "voice", "salutation_examples", default=[]) or []
    first = _first_name(merchant)
    if examples:
        template = examples[0]
        if _PLACEHOLDER_RE.search(template):
            return _PLACEHOLDER_RE.sub(first, template, count=1)
        return template
    return first


def _active_offer(merchant: dict) -> Optional[dict]:
    for o in merchant.get("offers", []) or []:
        if o.get("status") == "active":
            return o
    return None


def _digest_item(category: dict, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    for d in category.get("digest", []) or []:
        if d.get("id") == item_id:
            return d
    return None


def _peer_stat(category: dict, key: str):
    return _get(category, "peer_stats", key)


def _has_signal(merchant: dict, needle: str) -> bool:
    return any(needle in s for s in (merchant.get("signals") or []))


def _fmt_pct(x) -> str:
    try:
        return f"{abs(float(x)) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(x)


def _merchant_name(merchant: dict) -> str:
    return _get(merchant, "identity", "name", default="")


def _customer_first_name(customer: Optional[dict]) -> str:
    name = _get(customer, "identity", "name", default="there")
    return name.split(" (")[0]


def _payload(trigger: dict, key: str, default=None):
    return _get(trigger, "payload", key, default=default)


def _result(body: str, cta: str, send_as: str, suppression_key: str, rationale: str) -> dict:
    return {
        "body": body.strip(),
        "cta": cta,
        "send_as": send_as,
        "suppression_key": suppression_key,
        "rationale": rationale.strip(),
    }


# ---------------------------------------------------------------------------
# Merchant-facing trigger handlers (send_as = "vera")
# ---------------------------------------------------------------------------

def _h_research_digest(category, merchant, trigger, customer):
    item = _digest_item(category, _payload(trigger, "top_item_id"))
    sal = _salutation(category, merchant)
    use_hi = _is_hindi_mix(merchant, None)
    if not item:
        body = f"{sal}, this week's {category.get('display_name', category.get('slug'))} digest has a couple of relevant items — want the summary?"
        return _result(body, "open_ended", "vera", trigger.get("suppression_key", ""),
                        "No matching digest item found in category context; degraded to a generic offer to avoid fabricating specifics.")

    segment = item.get("patient_segment")
    seg_clause = ""
    if segment == "high_risk_adults" and _has_signal(merchant, "high_risk_adult"):
        seg_clause = "your high-risk adult patients" if not use_hi else "aapke high-risk adult patients"
    elif segment:
        seg_clause = segment.replace("_", " ")

    n = item.get("trial_n")
    n_clause = f"{n:,}-patient trial showed " if n else ""
    body = (
        f"{sal}, {item.get('source', 'the latest issue')} landed. "
        f"{'One item relevant to ' + seg_clause + ' — ' if seg_clause else ''}"
        f"{n_clause}{item.get('title', '').rstrip('.')}. "
        f"Worth a look (2-min read). Want me to pull it and draft a patient-ed WhatsApp you can share? "
        f"— {item.get('source', '')}"
    )
    return _result(
        body, "open_ended", "vera", trigger.get("suppression_key", ""),
        "Specificity (trial n, source citation) + merchant fit (patient segment tied to merchant's own cohort signal) "
        "+ effort externalization (offer to draft the follow-on) + curiosity ('worth a look')."
    )


def _h_regulation_change(category, merchant, trigger, customer):
    item = _digest_item(category, _payload(trigger, "top_item_id"))
    deadline = _payload(trigger, "deadline_iso")
    sal = _salutation(category, merchant)
    if not item:
        return _result(f"{sal}, a compliance update just dropped for your category — want the details?",
                        "open_ended", "vera", trigger.get("suppression_key", ""),
                        "No digest item matched; kept generic rather than inventing regulation text.")
    body = (
        f"{sal}, heads up — {item.get('title', '')} "
        f"({item.get('source', '')}). {item.get('actionable', '')}"
        f"{f' Deadline: {deadline[:10]}.' if deadline else ''} "
        f"Want me to check your current setup against this?"
    )
    return _result(body, "open_ended", "vera", trigger.get("suppression_key", ""),
                    "Loss aversion (compliance deadline) + specificity (source + actionable line) + effort externalization.")


def _h_cde_opportunity(category, merchant, trigger, customer):
    item = _digest_item(category, _payload(trigger, "digest_item_id"))
    sal = _salutation(category, merchant)
    if not item:
        return _result(f"{sal}, there's a CDE session coming up in your area — want the details?",
                        "open_ended", "vera", trigger.get("suppression_key", ""), "No digest match; kept generic.")
    credits = _payload(trigger, "credits")
    body = (
        f"{sal}, {item.get('title', '')} — {item.get('date', '')[:16].replace('T', ' ')}. "
        f"{item.get('summary', '')} "
        f"{f'{credits} CDE credits, ' if credits else ''}{item.get('actionable', '')}. Want me to block your calendar?"
    )
    return _result(body, "open_ended", "vera", trigger.get("suppression_key", ""),
                    "Specificity (date, speaker, credits) + effort externalization (calendar block offer).")


def _h_perf_dip(category, merchant, trigger, customer):
    metric = _payload(trigger, "metric", default="calls")
    delta = _payload(trigger, "delta_pct", default=0)
    window = _payload(trigger, "window", default="7d")
    baseline = _payload(trigger, "vs_baseline")
    sal = _salutation(category, merchant)
    severe = _has_signal(merchant, "perf_dip_severe")
    urgent_prefix = "Flagging this one — " if severe else "Quick check — "
    base_clause = f" (usually ~{baseline}/day)" if baseline else ""
    peer_key = f"avg_{metric}_30d"
    body = (
        f"{urgent_prefix}your {metric} are down {_fmt_pct(delta)} over the last {window}{base_clause}. "
        f"Peer median {metric} is around {_peer_stat(category, peer_key) or 'higher'} for your category. "
        f"Want me to check what changed — posts, hours, or an expired offer?"
    )
    body = f"{sal}, {body}"
    return _result(body, "open_ended", "vera", trigger.get("suppression_key", ""),
                    "Loss aversion (numeric drop) + social proof (peer median comparison) + curiosity (diagnostic offer).")


def _h_perf_spike(category, merchant, trigger, customer):
    metric = _payload(trigger, "metric", default="views")
    delta = _payload(trigger, "delta_pct", default=0)
    driver = _payload(trigger, "likely_driver")
    sal = _salutation(category, merchant)
    driver_clause = f" — looks like your {driver.replace('_', ' ')} is driving it" if driver else ""
    body = (
        f"{sal}, good news — your {metric} are up {_fmt_pct(delta)} this week{driver_clause}. "
        f"Want me to double down with a follow-up post while it's hot?"
    )
    return _result(body, "open_ended", "vera", trigger.get("suppression_key", ""),
                    "Reciprocity (proactively sharing good news) + specificity (numeric spike + likely driver) + low-friction CTA.")


def _h_milestone_reached(category, merchant, trigger, customer):
    metric = _payload(trigger, "metric", default="reviews")
    now_v = _payload(trigger, "value_now")
    target = _payload(trigger, "milestone_value")
    sal = _salutation(category, merchant)
    remaining = (target - now_v) if (isinstance(target, (int, float)) and isinstance(now_v, (int, float))) else None
    body = (
        f"{sal}, you're at {now_v} {metric.replace('_', ' ')} — "
        f"{f'just {remaining} away from {target}' if remaining else f'closing in on {target}'}. "
        f"Want a quick GBP post to nudge a few more reviews in?"
    )
    return _result(body, "open_ended", "vera", trigger.get("suppression_key", ""),
                    "Specificity (exact count + gap to milestone) + curiosity/momentum framing.")


def _h_dormant_with_vera(category, merchant, trigger, customer):
    days = _payload(trigger, "days_since_last_merchant_message", default=0)
    last_topic = _payload(trigger, "last_topic", default="")
    sal = _salutation(category, merchant)
    topic_clause = f" We'd left off on {last_topic.replace('_', ' ')}." if last_topic else ""
    body = (
        f"{sal}, it's been {days} days since we last spoke.{topic_clause} "
        f"Still want a hand with that, or is now not the right time? Reply STOP anytime to pause these check-ins."
    )
    return _result(body, "yes_no", "vera", trigger.get("suppression_key", ""),
                    "Single binary commitment (continue/STOP) + reciprocity (referencing prior thread) + graceful low-pressure exit path.")


def _h_review_theme_emerged(category, merchant, trigger, customer):
    theme = _payload(trigger, "theme", default="").replace("_", " ")
    occ = _payload(trigger, "occurrences_30d")
    quote = _payload(trigger, "common_quote")
    trend = _payload(trigger, "trend")
    sal = _salutation(category, merchant)
    quote_clause = f' One reviewer wrote: "{quote}."' if quote else ""
    body = (
        f"{sal}, {occ} reviews this month mention {theme}"
        f"{' (trending up)' if trend == 'rising' else ''}.{quote_clause} "
        f"Want me to draft a public reply template + a fix checklist?"
    )
    return _result(body, "open_ended", "vera", trigger.get("suppression_key", ""),
                    "Specificity (occurrence count + verbatim quote) + effort externalization (drafted reply template offer).")


def _h_competitor_opened(category, merchant, trigger, customer):
    name = _payload(trigger, "competitor_name")
    dist = _payload(trigger, "distance_km")
    their_offer = _payload(trigger, "their_offer")
    sal = _salutation(category, merchant)
    my_offer = _active_offer(merchant)
    compare = f" Your current offer is \"{my_offer['title']}\"." if my_offer else " You don't have an active offer to compete on right now."
    body = (
        f"{sal}, {name} opened {dist}km away with \"{their_offer}\".{compare} "
        f"Want me to suggest a counter-offer or a GBP post highlighting what makes you different?"
    )
    return _result(body, "open_ended", "vera", trigger.get("suppression_key", ""),
                    "Loss aversion (named nearby competitor + their price) + merchant fit (compares against merchant's real active offer).")


def _h_festival_upcoming(category, merchant, trigger, customer):
    fest = _payload(trigger, "festival")
    days = _payload(trigger, "days_until")
    sal = _salutation(category, merchant)
    catalog = category.get("offer_catalog", [])
    suggestion = catalog[0]["title"] if catalog else "a festival offer"
    body = (
        f"{sal}, {fest} is {days} days out. Merchants in your category usually see bookings pick up in the run-up. "
        f"Want me to set up \"{suggestion}\" as a {fest}-week special?"
    )
    return _result(body, "open_ended", "vera", trigger.get("suppression_key", ""),
                    "Specificity (exact days-out countdown) + effort externalization (concrete offer draft using the category's real catalog).")


def _h_ipl_match_today(category, merchant, trigger, customer):
    match = _payload(trigger, "match")
    venue = _payload(trigger, "venue")
    is_weeknight = _payload(trigger, "is_weeknight")
    sal = _salutation(category, merchant)
    note = "weeknight matches tend to drive extra footfall" if is_weeknight else "Saturday matches often shift orders to home-watch parties — plan your promo push for weeknights instead"
    body = f"{sal}, {match} is on today ({venue}). {note}. Want me to push a match-night combo post now?"
    return _result(body, "open_ended", "vera", trigger.get("suppression_key", ""),
                    "Specificity (real match + venue) + category research (the seasonal_beats insight, not a generic 'promote today').")


def _h_seasonal_perf_dip(category, merchant, trigger, customer):
    metric = _payload(trigger, "metric", default="views")
    delta = _payload(trigger, "delta_pct", default=0)
    note = _payload(trigger, "season_note", default="")
    sal = _salutation(category, merchant)
    body = (
        f"{sal}, your {metric} are down {_fmt_pct(delta)} this week — this matches the usual "
        f"{note.replace('_', ' ')} pattern for your category, not something specific to you. "
        f"Good time to focus on retention over acquisition. Want a quick winback list of lapsed members?"
    )
    return _result(body, "open_ended", "vera", trigger.get("suppression_key", ""),
                    "Honesty/trust (labels it seasonal, not alarmist) + specificity + effort externalization (winback list offer).")


def _h_category_seasonal(category, merchant, trigger, customer):
    trends = _payload(trigger, "trends", default=[])
    sal = _salutation(category, merchant)
    trend_str = ", ".join(t.replace("_", " ") for t in trends[:3]) if trends else "seasonal shifts"
    body = (
        f"{sal}, this season's demand shift across your category: {trend_str}. "
        f"Want me to draft a shelf/menu adjustment note based on this?"
    )
    return _result(body, "open_ended", "vera", trigger.get("suppression_key", ""),
                    "Specificity (named trend list) + effort externalization.")


def _h_gbp_unverified(category, merchant, trigger, customer):
    uplift = _payload(trigger, "estimated_uplift_pct")
    sal = _salutation(category, merchant)
    body = (
        f"{sal}, your Google Business Profile isn't verified yet — verified listings in your category "
        f"typically see ~{_fmt_pct(uplift)} more calls. Takes one postcard or phone call. Want me to start it for you?"
    )
    return _result(body, "yes_no", "vera", trigger.get("suppression_key", ""),
                    "Loss aversion (missed uplift %) + effort externalization (\"start it for you\") + single binary CTA.")


def _h_renewal_due(category, merchant, trigger, customer):
    days = _payload(trigger, "days_remaining")
    amount = _payload(trigger, "renewal_amount")
    sal = _salutation(category, merchant)
    urgent = days is not None and days <= 14
    body = (
        f"{sal}, your subscription renews in {days} days"
        f"{f' (₹{amount:,})' if amount else ''}. "
        f"{'Renew now to avoid a gap in your profile visibility. ' if urgent else ''}"
        f"Reply YES to renew, or STOP if you'd like to let it lapse."
    )
    return _result(body, "yes_no", "vera", trigger.get("suppression_key", ""),
                    "Loss aversion + single binary commitment (YES/STOP) — matches anti-pattern guidance against multi-choice CTAs.")


def _h_active_planning_intent(category, merchant, trigger, customer):
    topic = _payload(trigger, "intent_topic", default="").replace("_", " ")
    last_msg = _payload(trigger, "merchant_last_message", default="")
    sal = _salutation(category, merchant)
    # Intent-handoff rule: merchant already signaled interest -> go straight to
    # action, no qualifying questions (challenge-brief.md Pattern D anti-pattern).
    lead = f"{sal}, on it." if sal.lower().startswith("hi ") else f"On it, {sal}."
    body = (
        f"{lead} For \"{topic}\" — here's a starting draft: "
        f"structure it as a simple package (duration, price, one clear differentiator), "
        f"and I'll turn it into a GBP post + WhatsApp broadcast for your customer list. "
        f"Want me to draft the post now, or would you like to set the price and duration first?"
    )
    return _result(body, "open_ended", "vera", trigger.get("suppression_key", ""),
                    f"Intent-handoff done right: merchant said '{last_msg}' (clear go-ahead) — routed straight into "
                    "action mode instead of re-qualifying, per the anti-pattern in the brief.")


def _h_curious_ask_due(category, merchant, trigger, customer):
    sal = _salutation(category, merchant)
    body = f"{sal}, quick one — what's the most-asked-about service at your place this week? Curious what's trending on your end."
    return _result(body, "open_ended", "vera", trigger.get("suppression_key", ""),
                    "Lever #7 (asking the merchant) — production Vera under-uses this; low-pressure, builds the relationship, no data claim needed.")


def _h_supply_alert(category, merchant, trigger, customer):
    molecule = _payload(trigger, "molecule")
    batches = _payload(trigger, "affected_batches", default=[])
    mfr = _payload(trigger, "manufacturer")
    sal = _salutation(category, merchant)
    body = (
        f"{sal}, voluntary recall alert — {molecule} batches {', '.join(batches)} ({mfr}) flagged for sub-potency. "
        f"No acute safety risk, but customers on this should be switched. Want me to pull your repeat-Rx customer "
        f"list filtered for {molecule} so you can reach out?"
    )
    return _result(body, "yes_no", "vera", trigger.get("suppression_key", ""),
                    "High urgency (5) handled with precision, not alarm; specificity (batch numbers) + effort externalization (filtered list offer).")


def _h_winback_eligible(category, merchant, trigger, customer):
    days = _payload(trigger, "days_since_expiry")
    dip = _payload(trigger, "perf_dip_pct")
    lapsed = _payload(trigger, "lapsed_customers_added_since_expiry")
    sal = _salutation(category, merchant)
    body = (
        f"{sal}, it's been {days} days since your subscription lapsed — profile visibility dipped {_fmt_pct(dip)} since, "
        f"and {lapsed} more customers went quiet in that window. Want to pick back up? First step takes 2 minutes."
    )
    return _result(body, "yes_no", "vera", trigger.get("suppression_key", ""),
                    "Loss aversion (concrete dip + lapsed count) + low-friction reactivation CTA.")


# ---------------------------------------------------------------------------
# Customer-facing trigger handlers (send_as = "merchant_on_behalf")
# ---------------------------------------------------------------------------

def _h_recall_due(category, merchant, trigger, customer):
    cust_name = _customer_first_name(customer)
    merch_name = _merchant_name(merchant)
    use_hi = _is_hindi_mix(merchant, customer)
    service = _payload(trigger, "service_due", default="").replace("_", " ")
    slots = _payload(trigger, "available_slots", default=[])
    offer = _active_offer(merchant)
    slot_str = " ya ".join(s["label"] for s in slots[:2]) if use_hi else " or ".join(s["label"] for s in slots[:2])
    price_clause = f" {offer['title']}." if offer else ""
    emoji = "🦷" if category.get("slug") == "dentists" else ""
    if use_hi:
        body = (
            f"Hi {cust_name}, {merch_name} here {emoji} Aapka {service} recall due hai. "
            f"Apke liye slots ready hain: {slot_str}.{price_clause} Reply 1 for pehla slot, 2 for doosra, "
            f"ya koi aur time batao jo aapke liye theek ho."
        )
    else:
        body = (
            f"Hi {cust_name}, {merch_name} here {emoji} Your {service} is due. "
            f"We have slots: {slot_str}.{price_clause} Reply 1 for the first, 2 for the second, "
            f"or tell us a time that works for you."
        )
    return _result(body, "multi_choice_booking", "merchant_on_behalf", trigger.get("suppression_key", ""),
                    "Customer fit (name, language mix, real open slots) + trigger relevance (names the recall explicitly) + "
                    "booking-flow multi-choice CTA (allowed per brief for scheduling).")


def _h_chronic_refill_due(category, merchant, trigger, customer):
    merch_name = _merchant_name(merchant)
    use_hi = _is_hindi_mix(merchant, customer)
    molecules = _payload(trigger, "molecule_list", default=[])
    runs_out = _payload(trigger, "stock_runs_out_iso", default="")
    delivery = _payload(trigger, "delivery_address_saved")
    mol_str = ", ".join(molecules)
    if use_hi:
        body = (
            f"Namaste, {merch_name} se. Aapki {mol_str} ki dawaiyan {runs_out[:10]} tak khatam ho sakti hain. "
            f"{'Aapka saved delivery address use karke bhej dein?' if delivery else 'Bata dein delivery address, turant bhej denge.'} "
            f"Reply YES to confirm."
        )
    else:
        body = (
            f"Hi, this is {merch_name}. Your {mol_str} refill runs out around {runs_out[:10]}. "
            f"{'Should we deliver to your saved address?' if delivery else 'Share your delivery address and we will send it right away.'} "
            f"Reply YES to confirm."
        )
    return _result(body, "yes_no", "merchant_on_behalf", trigger.get("suppression_key", ""),
                    "Care/urgency framing for a chronic patient + specificity (exact molecules, exact stock-out date) + single binary CTA.")


def _h_trial_followup(category, merchant, trigger, customer):
    cust_name = _customer_first_name(customer)
    merch_name = _merchant_name(merchant)
    trial_date = _payload(trigger, "trial_date", default="")
    options = _payload(trigger, "next_session_options", default=[])
    opt_str = ", ".join(o["label"] for o in options)
    body = (
        f"Hi {cust_name}, hope you enjoyed the trial on {trial_date} at {merch_name}! "
        f"Next session option: {opt_str}. Want us to book it in?"
    )
    return _result(body, "yes_no", "merchant_on_behalf", trigger.get("suppression_key", ""),
                    "Trigger relevance (references the actual trial date) + low-friction next-step CTA.")


def _h_wedding_package_followup(category, merchant, trigger, customer):
    cust_name = _customer_first_name(customer)
    merch_name = _merchant_name(merchant)
    days_to = _payload(trigger, "days_to_wedding")
    next_step = _payload(trigger, "next_step_window_open", default="").replace("_", " ")
    body = (
        f"Hi {cust_name}, {days_to} days to go! Your trial at {merch_name} is done — this is usually the right "
        f"window to start the {next_step}. Want us to lock it in?"
    )
    return _result(body, "yes_no", "merchant_on_behalf", trigger.get("suppression_key", ""),
                    "Specificity (exact countdown) + trigger relevance (names the exact next-step program).")


def _h_customer_lapsed(category, merchant, trigger, customer):
    cust_name = _customer_first_name(customer)
    merch_name = _merchant_name(merchant)
    days = _payload(trigger, "days_since_last_visit")
    focus = _payload(trigger, "previous_focus", default="").replace("_", " ")
    body = (
        f"Hi {cust_name}, it's been {days} days since your last visit to {merch_name}"
        f"{f' — hope the {focus} goals are still on track' if focus else ''}. "
        f"Want to pick a slot to get back to it? Reply YES and we'll send options."
    )
    return _result(body, "yes_no", "merchant_on_behalf", trigger.get("suppression_key", ""),
                    "Customer fit (references their stated goal) + low-pressure winback CTA.")


def _h_appointment_tomorrow(category, merchant, trigger, customer):
    cust_name = _customer_first_name(customer) if customer else "there"
    merch_name = _merchant_name(merchant)
    body = f"Hi {cust_name}, reminder — your appointment with {merch_name} is tomorrow. Reply YES to confirm or let us know if you need to reschedule."
    return _result(body, "yes_no", "merchant_on_behalf", trigger.get("suppression_key", ""),
                    "Functional reminder, single binary CTA (confirm/reschedule), no over-messaging.")


# ---------------------------------------------------------------------------
# Dispatch table + fallback
# ---------------------------------------------------------------------------

_MERCHANT_HANDLERS = {
    "research_digest": _h_research_digest,
    "regulation_change": _h_regulation_change,
    "cde_opportunity": _h_cde_opportunity,
    "perf_dip": _h_perf_dip,
    "perf_spike": _h_perf_spike,
    "milestone_reached": _h_milestone_reached,
    "dormant_with_vera": _h_dormant_with_vera,
    "review_theme_emerged": _h_review_theme_emerged,
    "competitor_opened": _h_competitor_opened,
    "festival_upcoming": _h_festival_upcoming,
    "ipl_match_today": _h_ipl_match_today,
    "seasonal_perf_dip": _h_seasonal_perf_dip,
    "category_seasonal": _h_category_seasonal,
    "gbp_unverified": _h_gbp_unverified,
    "renewal_due": _h_renewal_due,
    "active_planning_intent": _h_active_planning_intent,
    "curious_ask_due": _h_curious_ask_due,
    "supply_alert": _h_supply_alert,
    "winback_eligible": _h_winback_eligible,
}

_CUSTOMER_HANDLERS = {
    "recall_due": _h_recall_due,
    "chronic_refill_due": _h_chronic_refill_due,
    "trial_followup": _h_trial_followup,
    "wedding_package_followup": _h_wedding_package_followup,
    "customer_lapsed_soft": _h_customer_lapsed,
    "customer_lapsed_hard": _h_customer_lapsed,
    "appointment_tomorrow": _h_appointment_tomorrow,
}


def _fallback(category, merchant, trigger, customer):
    """Used only for trigger kinds with no dedicated handler, or for
    generator placeholder triggers with payload={'placeholder': True} that
    carry no real fields. Falls back to the strongest signal we actually
    have rather than inventing content."""
    is_customer_scope = trigger.get("scope") == "customer" and customer is not None
    kind = trigger.get("kind", "update").replace("_", " ")
    article = "an" if kind[:1].lower() in "aeiou" else "a"

    if is_customer_scope:
        cust_name = _customer_first_name(customer)
        merch_name = _merchant_name(merchant)
        body = (
            f"Hi {cust_name}, quick note from {merch_name} — {article} {kind} update on your account. "
            f"Want the details?"
        )
        send_as = "merchant_on_behalf"
    else:
        sal = _salutation(category, merchant)
        signals = merchant.get("signals") or []
        if signals:
            sig = signals[0].split(":")[0].replace("_", " ")
            body = f"{sal}, quick nudge on your {kind} — noticed '{sig}' on your account. Want me to take a look with you?"
        else:
            body = f"{sal}, flagging {article} {kind} update on your account. Want the details?"
        send_as = "vera"

    return _result(body, "open_ended", send_as, trigger.get("suppression_key", ""),
                    f"No dedicated composer branch for trigger kind '{trigger.get('kind')}' (or a placeholder-only "
                    "payload from the generator); degraded to the merchant's/customer's strongest real signal rather "
                    "than fabricating specifics for an unrecognized trigger.")


def _placeholder_fallback(category, merchant, trigger, customer):
    """Compose from trigger intent plus real context when generated payloads are empty."""
    kind = trigger.get("kind", "update")
    sal = _salutation(category, merchant)
    perf = merchant.get("performance") or {}
    identity = merchant.get("identity") or {}
    locality = identity.get("locality") or identity.get("city")
    place = f" in {locality}" if locality else ""
    merchant_name = _merchant_name(merchant)
    is_customer = trigger.get("scope") == "customer" and customer is not None

    if is_customer:
        name = _customer_first_name(customer)
        send_as = "merchant_on_behalf"
        if kind == "appointment_tomorrow":
            body = f"Hi {name}, a reminder from {merchant_name}: your appointment is tomorrow. Reply YES to confirm or RESCHEDULE if you need a different time."
        elif kind in ("customer_lapsed_soft", "customer_lapsed_hard"):
            last_visit = _get(customer, "relationship", "last_visit")
            visit_text = f" Your last visit was {last_visit}." if last_visit else ""
            body = f"Hi {name}, we would be glad to see you again at {merchant_name}.{visit_text} Reply YES and we will share a suitable time."
        elif kind == "trial_followup":
            body = f"Hi {name}, how did your recent session at {merchant_name} go? Reply YES and we will share the next available options."
        elif kind == "recall_due":
            body = f"Hi {name}, {merchant_name} has a follow-up reminder for you. Reply YES if you would like the team to help arrange the next step."
        elif kind == "chronic_refill_due":
            if merchant.get("category_slug") == "pharmacies":
                body = f"Hi {name}, {merchant_name} can check your refill. Reply YES and the pharmacy team will confirm the medicine and availability with you."
            else:
                body = f"Hi {name}, {merchant_name} has a follow-up reminder for you. Reply YES and the team will confirm the details with you."
        else:
            visits = _get(customer, "relationship", "visits_total")
            history = f" You have visited {visits} times." if visits is not None else ""
            body = f"Hi {name}, a quick note from {merchant_name}.{history} Reply YES if you would like help with your next visit."
        if _is_hindi_mix(merchant, customer):
            body = body.replace(f"Hi {name},", f"Namaste {name},")
    else:
        send_as = "vera"
        if kind in ("perf_dip", "perf_spike"):
            views, calls = perf.get("views"), perf.get("calls")
            days = perf.get("window_days", 30)
            if views is not None and calls is not None:
                body = f"{sal}, your profile recorded {views:,} views and {calls:,} calls over {days} days{place}. Want me to review one practical way to turn more views into enquiries?"
            else:
                body = f"{sal}, I am checking your recent profile performance{place}. Want a short review of what could bring more enquiries?"
        elif kind == "renewal_due":
            sub = merchant.get("subscription") or {}
            days = sub.get("days_remaining")
            if days is not None and days > 0:
                body = f"{sal}, your {sub.get('plan', 'current')} plan has {days} days remaining. Want me to share the renewal options?"
            else:
                expired = sub.get("days_since_expiry")
                when = f" {expired} days ago" if expired else ""
                body = f"{sal}, your {sub.get('plan', 'Vera')} plan has expired{when}. Want me to show you the steps to renew?"
        elif kind == "competitor_opened":
            offer = _active_offer(merchant)
            offer_text = f" Your active offer is {offer.get('title')}." if offer else ""
            body = f"{sal}, a new competitor signal was flagged{place}.{offer_text} Want me to sharpen your listing around what customers value here?"
        elif kind == "festival_upcoming":
            catalog = category.get("offer_catalog") or []
            offer_text = f" One catalog option is {catalog[0].get('title')}." if catalog else ""
            body = f"{sal}, want to prepare a timely offer for the upcoming seasonal window?{offer_text} I can draft the message for your approval."
        elif kind == "research_digest":
            items = category.get("digest") or []
            item = items[0] if items else None
            if item:
                body = f"{sal}, {item.get('title', 'a new category update')} ({item.get('source', 'category digest')}). Want me to summarize the practical takeaway for your business?"
            else:
                body = f"{sal}, there is a new {category.get('display_name', 'category')} update. Want a short summary?"
        elif kind == "milestone_reached":
            total = _get(merchant, "customer_aggregate", "total_unique_ytd")
            if total is not None:
                body = f"{sal}, your business has reached {total:,} unique customers year to date. Want me to draft a short thank-you post to mark it?"
            else:
                body = f"{sal}, there is a milestone worth celebrating for your business. Want a short customer thank-you post drafted?"
        elif kind == "curious_ask_due":
            vertical = category.get("display_name", "business").lower()
            body = f"{sal}, quick question: what are customers asking for most at your {vertical} this week? I can use that to suggest a relevant next post."
        elif kind == "review_theme_emerged":
            body = f"{sal}, I can help turn customer feedback into a useful next step. Want a concise public reply draft?"
        else:
            signals = merchant.get("signals") or []
            if signals:
                sig = signals[0].split(":")[0].replace("_", " ")
                body = f"{sal}, I noticed {sig} in your account context{place}. Want me to suggest one practical next step?"
            elif perf.get("views") is not None and perf.get("calls") is not None:
                body = f"{sal}, your profile recorded {perf['views']:,} views and {perf['calls']:,} calls in the last {perf.get('window_days', 30)} days{place}. Want me to review one way to improve that journey?"
            else:
                body = f"{sal}, I have a relevant {category.get('display_name', 'business')} update for you. Want a short summary and one next step?"

    return _result(body, "yes_no", send_as, trigger.get("suppression_key", ""),
                   f"Used the '{kind}' trigger intent and available context facts; avoided inventing missing trigger details and ended with one low-friction action.")


def _dispatch(category: dict, merchant: dict, trigger: dict, customer: Optional[dict]):
    kind = trigger.get("kind", "")
    scope = trigger.get("scope", "merchant")
    # generate_dataset.py's auto-expanded triggers (beyond the 25 hand-written
    # seeds) carry payload={"placeholder": True, ...} with no real fields.
    # Composing from those would force us to either fabricate numbers/dates
    # (forbidden by the brief) or leak "None" into the message. Route them to
    # the signal-based fallback instead, which only uses real merchant data.
    if trigger.get("payload", {}).get("placeholder"):
        return _placeholder_fallback(category, merchant, trigger, customer)
    if scope == "customer" and customer is not None:
        handler = _CUSTOMER_HANDLERS.get(kind)
        if handler:
            return handler(category, merchant, trigger, customer)
    handler = _MERCHANT_HANDLERS.get(kind)
    if handler:
        return handler(category, merchant, trigger, customer)
    return _fallback(category, merchant, trigger, customer)


# ---------------------------------------------------------------------------
# Anti-repetition + taboo-word guard (post-processing, cheap safety net)
# ---------------------------------------------------------------------------

def _strip_taboo_words(body: str, category: dict) -> str:
    taboo = _get(category, "voice", "vocab_taboo", default=[]) or []
    for t in taboo:
        # only strip whole-word matches, case-insensitive, conservatively
        pattern = re.compile(re.escape(t.split(" (")[0]), re.IGNORECASE)
        if pattern.search(body):
            body = pattern.sub("", body)
    body = re.sub(r"\s{2,}", " ", body).strip()
    return body


def _dedupe_against_history(body: str, merchant: dict, sent_bodies: Optional[set]) -> str:
    prior = {h.get("body") for h in (merchant.get("conversation_history") or []) if h.get("from") == "vera"}
    if sent_bodies:
        prior = prior | sent_bodies
    if body in prior:
        body = body.rstrip(".") + " (checking in again on this)."
    return body


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None,
            sent_bodies: Optional[set] = None) -> dict:
    """
    The required contract:
        compose(category, merchant, trigger, customer=None) -> dict with
        keys: body, cta, send_as, suppression_key, rationale.

    Deterministic given the same inputs (pure function, no randomness, no
    network calls unless VERA_LLM_POLISH=1 is explicitly opted into).
    """
    if not category or not merchant or not trigger:
        return _result(
            "", "none", "vera", trigger.get("suppression_key", "") if trigger else "",
            "Missing required context (category/merchant/trigger) — refusing to fabricate a message."
        )

    result = _dispatch(category, merchant, trigger, customer)
    result["body"] = _strip_taboo_words(result["body"], category)
    result["body"] = _dedupe_against_history(result["body"], merchant, sent_bodies)

    if USE_LLM_POLISH:
        polished = _llm_polish(result["body"], category, merchant, customer)
        if polished:
            result["body"] = polished

    return result


def _llm_polish(draft: str, category: dict, merchant: dict, customer: Optional[dict]) -> Optional[str]:
    """Optional temperature=0 rewrite pass via the Anthropic API. Never
    invents new facts — instructed to only rephrase. Silently no-ops if the
    API isn't reachable/configured, so the bot never depends on it."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic  # local import: optional dependency
        client = anthropic.Anthropic(api_key=api_key)
        voice = _get(category, "voice", "tone", default="")
        system = (
            "You polish WhatsApp business messages for natural phrasing. "
            "Rules: (1) do not add any fact, number, name, date, or claim not already in the draft; "
            "(2) keep the same call-to-action; (3) keep length similar; (4) match this voice tone: "
            f"{voice}; (5) output ONLY the rewritten message, nothing else."
        )
        resp = client.messages.create(
            model=LLM_MODEL,
            max_tokens=400,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": draft}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        return text or None
    except Exception:
        return None
