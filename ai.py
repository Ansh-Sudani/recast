"""Draft the narrative parts of a CIM with Claude. Numbers come from engine.py;
the model only writes prose around them and is told not to invent figures."""

import json
import os

MODEL = os.environ.get("RECAST_MODEL", "claude-sonnet-5-5")

SYSTEM = """You are a sell-side analyst at a Canadian business brokerage drafting a
Confidential Information Memorandum (CIM) for a Main Street business.
Rules:
- Use ONLY the figures provided. Never invent revenue, customers, staff counts or dates.
- If something a buyer would want isn't in the notes, list it under missing_info instead of guessing.
- Never describe things the notes don't state: staff quality or experience, customer loyalty,
  payment methods, competition, reputation. If unsure, leave it out.
- Do no arithmetic of your own. Quote only the figures given, and lead with the latest year.
- People or expenses listed as addbacks are assumed to LEAVE with the sale. Never present them
  as an asset, a growth lever, or part of the transition.
- Plain, confident, specific prose. No hype words ("unparalleled", "turnkey goldmine").
- Canadian spelling and CAD.
Return strict JSON with keys:
executive_summary (string, 2 short paragraphs),
business_overview (string),
operations (string),
growth_opportunities (list of 3-5 strings),
investment_highlights (list of 4-6 strings),
buyer_questions (list of 5-8 questions a sharp buyer will ask, based on the numbers and notes),
missing_info (list of strings: documents/facts the broker should collect before marketing)."""


def _fmt(n):
    return f"${n:,.0f}"


def _facts(biz, m, val):
    yrs = m["years"]
    rows = "\n".join(
        f"{yrs[i]}: revenue {_fmt(m['revenue'][i])}, gross margin {m['gross_margin'][i]:.1f}%, "
        f"net income {_fmt(m['net_income'][i])}, SDE {_fmt(m['sde'][i])}"
        for i in range(len(yrs)))
    return (f"Business: {biz.get('name') or 'Unnamed'}\nIndustry: {biz.get('industry')}\n"
            f"Location: {biz.get('location')}\nAsking price: {biz.get('asking') or 'not set'}"
            f"{' (' + biz['_asking_mult'] + ' weighted SDE)' if biz.get('_asking_mult') else ''}\n\n"
            f"Financials (recast):\n{rows}\nWeighted SDE: {_fmt(m['weighted_sde'])}\n"
            f"Indicative value range: {_fmt(val['low'])} - {_fmt(val['high'])}\n\n"
            f"Addbacks (assumed to go away after the sale): {biz.get('_addbacks') or 'none'}\n\n"
            f"Seller / broker notes:\n{biz.get('notes') or '(none)'}")


GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")


def _groq(key, prompt):
    """Groq's free tier, via its OpenAI-compatible endpoint. No extra package needed."""
    import urllib.error
    import urllib.request
    body = json.dumps({"model": GROQ_MODEL, "temperature": 0.3,
                       "response_format": {"type": "json_object"},
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          # Groq sits behind Cloudflare, which blocks Python's default user agent
                                          "User-Agent": "recast/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        print(f"[recast] Groq error {e.code}: {detail}", flush=True)
        raise RuntimeError(f"Groq HTTP {e.code}: {detail[:120]}") from None


def draft_cim(biz, m, val):
    akey, gkey = os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("GROQ_API_KEY")
    if not (akey or gkey):
        return _fallback(biz, m, val), "template"
    try:
        if akey:
            import anthropic
            msg = anthropic.Anthropic(api_key=akey).messages.create(
                model=MODEL, max_tokens=2500, system=SYSTEM,
                messages=[{"role": "user", "content": _facts(biz, m, val)}])
            text = "".join(b.text for b in msg.content if b.type == "text").strip()
        else:
            text = _groq(gkey, _facts(biz, m, val)).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return _scrub(json.loads(text), biz), "claude"
    except Exception as e:  # never break the report because the model call failed
        d = _fallback(biz, m, val)
        d["missing_info"].insert(0, f"(AI draft unavailable: {e}. Showing template text.)")
        return d, "template"


def _scrub(d, biz):
    """Hard guard: drop selling points that lean on people/expenses we added back
    (e.g. 'use the spouse's social media skills'). Models don't always obey the prompt."""
    import re
    words = set(re.findall(r"\b(?:spouse|wife|husband|family|son|daughter)\b",
                           (biz.get("_addbacks") or "").lower()))
    if not words:
        return d
    pat = re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)
    for k in ("growth_opportunities", "investment_highlights"):
        if isinstance(d.get(k), list):
            d[k] = [x for x in d[k] if not pat.search(str(x))]
    return d


def _fallback(biz, m, val):
    name = biz.get("name") or "The Business"
    last = len(m["years"]) - 1
    g = m["revenue_growth"][last]
    trend = f"{'up' if g >= 0 else 'down'} {abs(g):.1f}% year over year" if g is not None else "in its latest year"
    return {
        "executive_summary": (
            f"{name} is a {biz.get('industry') or 'small business'} based in {biz.get('location') or 'Ontario'}. "
            f"In {m['years'][last]} it generated {_fmt(m['revenue'][last])} in revenue, {trend}, "
            f"and {_fmt(m['sde'][last])} in seller's discretionary earnings.\n\n"
            f"On a weighted basis SDE is {_fmt(m['weighted_sde'])}, supporting an indicative range of "
            f"{_fmt(val['low'])} to {_fmt(val['high'])} before working capital and real estate."),
        "business_overview": biz.get("notes") or "Add seller notes to generate an overview.",
        "operations": "Describe staffing, hours, suppliers, lease terms and owner involvement here.",
        "growth_opportunities": ["Extend hours or add a second revenue line",
                                 "Online ordering / delivery partnerships",
                                 "Raise prices in line with local competitors"],
        "investment_highlights": [f"{_fmt(m['weighted_sde'])} weighted SDE",
                                  f"{m['gross_margin'][last]:.0f}% gross margin",
                                  f"{len(m['years'])} years of financial history provided"],
        "buyer_questions": ["How many hours a week does the owner work, and in what role?",
                            "What are the lease term, renewal options and rent escalations?",
                            "Can each addback be supported with receipts or statements?",
                            "Who are the top suppliers and are prices locked?"],
        "missing_info": ["T2 returns or Notices of Assessment matching the P&L",
                         "Lease agreement", "12 months of bank statements",
                         "Staff list with tenure and wages", "Equipment list with ages"],
    }
