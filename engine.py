"""Recast engine: parse a small-business P&L, find addbacks, compute SDE/EBITDA,
flag risks. Pure Python, no AI — every number here is deterministic and traceable
to a line in the uploaded file."""

import csv
import io
import re

# ---------------------------------------------------------------- parsing

SKIP_ROW = re.compile(r"\b(total|gross profit|net income|net profit|net loss|ebitda|subtotal)\b", re.I)
REVENUE_KW = re.compile(r"\b(revenue|sales|income from operations|fees earned|service income)\b", re.I)
COGS_KW = re.compile(r"\b(cost of (goods|sales)|cogs|purchases|inventory|materials|food cost|subcontract)", re.I)
OTHER_INCOME_KW = re.compile(r"\b(other income|interest income|gain on)", re.I)


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("$", "").replace(",", "").replace("CAD", "").strip()
    if not s or s in {"-", "—"}:
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if neg else n


def _rows_from_upload(filename, raw_bytes):
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
        ws = wb.worksheets[0]
        return [[c for c in row] for row in ws.iter_rows(values_only=True)]
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    return list(csv.reader(io.StringIO(text)))


def parse_pnl(filename, raw_bytes):
    """Returns dict(years=[...], lines=[{name, category, values:[...]}])."""
    rows = [r for r in _rows_from_upload(filename, raw_bytes) if any(c not in (None, "") for c in r)]
    if not rows:
        raise ValueError("The file is empty.")

    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    cat_idx = next((i for i, h in enumerate(header) if h.lower() in {"category", "type", "section"}), None)
    year_idx = [i for i, h in enumerate(header) if i != 0 and i != cat_idx and h]
    if not year_idx:
        raise ValueError("Couldn't find year columns. First row should be: Line Item, [Category], FY2023, FY2024, ...")
    years = [header[i] for i in year_idx]

    lines = []
    section = None
    for r in rows[1:]:
        r = list(r) + [None] * (len(header) - len(r))
        name = str(r[0]).strip() if r[0] is not None else ""
        if not name:
            continue
        vals = [_num(r[i]) for i in year_idx]
        # Section header rows like "Revenue" / "Expenses" with no numbers
        if all(v is None for v in vals):
            low = name.lower()
            if "revenue" in low or "sales" in low or "income" == low:
                section = "revenue"
            elif "cost of" in low or "cogs" in low:
                section = "cogs"
            elif "expense" in low or "operating" in low:
                section = "expense"
            elif "other" in low:
                section = "other_income"
            continue
        if SKIP_ROW.search(name):
            continue
        vals = [v or 0.0 for v in vals]

        cat = None
        if cat_idx is not None and r[cat_idx]:
            c = str(r[cat_idx]).strip().lower()
            cat = ("revenue" if c.startswith("rev") or c.startswith("sale")
                   else "cogs" if c in {"cogs", "cost of goods sold", "cost of sales", "direct costs"}
                   else "other_income" if "other inc" in c
                   else "expense")
        if cat is None:
            if OTHER_INCOME_KW.search(name):
                cat = "other_income"
            elif REVENUE_KW.search(name) and not re.search(r"expense|cost", name, re.I):
                cat = "revenue"
            elif COGS_KW.search(name):
                cat = "cogs"
            else:
                cat = section or "expense"
        lines.append({"name": name, "category": cat, "values": vals})

    if not any(l["category"] == "revenue" for l in lines):
        raise ValueError("No revenue lines found. Add a Category column (Revenue / COGS / Expense).")
    return {"years": years, "lines": lines}


# ---------------------------------------------------------------- addbacks

# (pattern, type, default share added back, confidence, why buyers accept/question it)
ADDBACK_RULES = [
    (r"owner|officer|shareholder|management fee|director'?s? (fee|salary)|principal", "Owner compensation", 1.0, "standard",
     "Owner pay is replaced by the buyer's own earnings, so it's added back for SDE."),
    (r"depreciation|amortization|amortisation|\bcca\b|capital cost allowance", "Depreciation & amortization", 1.0, "standard",
     "Non-cash accounting expense."),
    (r"interest (expense|on|paid)|loan interest|bank interest|mortgage interest|^interest$", "Interest", 1.0, "standard",
     "Financing cost of the current owner; buyer brings their own financing."),
    (r"income tax|corporate tax", "Income taxes", 1.0, "standard", "SDE is pre-tax."),
    (r"donation|charit", "Charitable donations", 1.0, "standard", "Discretionary, not needed to run the business."),
    (r"vehicle|auto|car |car$|fuel|gas\b", "Vehicle", 0.5, "verify",
     "Often partly personal. Buyers will ask for a logbook; 50% is a placeholder."),
    (r"meals|entertainment", "Meals & entertainment", 0.5, "verify",
     "Partly discretionary. Needs receipts to support."),
    (r"personal|family|spouse", "Personal / family", 1.0, "verify",
     "Only valid if the person/expense isn't needed post-sale."),
    (r"one[- ]time|non[- ]recurring|settlement|legal.*(dispute|lawsuit)|relocation|moving|renovation", "One-time", 1.0, "verify",
     "Buyers accept these only with documentation showing it won't recur."),
    (r"life insurance|key ?person", "Owner life insurance", 1.0, "verify", "Owner-specific policy."),
    (r"travel", "Travel", 0.0, "verify", "Usually operating. Add back only the personal share."),
]


def find_addbacks(lines):
    out = []
    for idx, l in enumerate(lines):
        if l["category"] not in ("expense", "cogs"):
            continue
        for pat, typ, share, conf, why in ADDBACK_RULES:
            if re.search(pat, l["name"], re.I):
                out.append({
                    "line_index": idx,
                    "name": l["name"],
                    "type": typ,
                    "share": share,
                    "confidence": conf,
                    "why": why,
                    "values": [round(v * share, 2) for v in l["values"]],
                    "raw_values": l["values"],
                    "enabled": share > 0,
                })
                break
    return out


# ---------------------------------------------------------------- financials

def _sum(lines, cat, n):
    return [sum(l["values"][i] for l in lines if l["category"] == cat) for i in range(n)]


def compute(pnl, addbacks=None):
    years, lines = pnl["years"], pnl["lines"]
    n = len(years)
    if addbacks is None:
        addbacks = find_addbacks(lines)

    revenue = _sum(lines, "revenue", n)
    cogs = _sum(lines, "cogs", n)
    opex = _sum(lines, "expense", n)
    other = _sum(lines, "other_income", n)
    gross = [revenue[i] - cogs[i] for i in range(n)]
    net = [gross[i] - opex[i] + other[i] for i in range(n)]

    enabled = [a for a in addbacks if a["enabled"]]
    addback_total = [sum(a["values"][i] for a in enabled) for i in range(n)]
    ebitda_types = {"Depreciation & amortization", "Interest", "Income taxes"}
    ebitda = [net[i] + sum(a["values"][i] for a in enabled if a["type"] in ebitda_types) for i in range(n)]
    sde = [net[i] + addback_total[i] for i in range(n)]

    # 3-2-1 weighting, latest year heaviest (years assumed oldest -> newest)
    weights = list(range(1, n + 1))
    weighted_sde = sum(sde[i] * weights[i] for i in range(n)) / sum(weights)

    def pct(a, b):
        return (a / b * 100) if b else 0.0

    metrics = {
        "years": years,
        "revenue": revenue, "cogs": cogs, "gross_profit": gross, "opex": opex,
        "other_income": other, "net_income": net, "ebitda": ebitda,
        "addbacks_total": addback_total, "sde": sde, "weighted_sde": weighted_sde,
        "gross_margin": [pct(gross[i], revenue[i]) for i in range(n)],
        "sde_margin": [pct(sde[i], revenue[i]) for i in range(n)],
        "revenue_growth": [None] + [pct(revenue[i] - revenue[i - 1], revenue[i - 1]) for i in range(1, n)],
    }
    metrics["flags"] = risk_flags(metrics, addbacks)
    return metrics, addbacks


def valuation(weighted_sde, low=2.0, mid=2.5, high=3.0):
    return {"low": weighted_sde * low, "mid": weighted_sde * mid, "high": weighted_sde * high,
            "multiples": {"low": low, "mid": mid, "high": high}}


def asking_flag(asking_text, metrics, val):
    """Compare a typed asking price against the indicative range."""
    n = _num(re.sub(r"[^\d.,()-]", "", asking_text or ""))
    w = metrics["weighted_sde"]
    if not n or w <= 0:
        return
    mult = n / w
    if n > val["high"] * 1.05:
        metrics["flags"].insert(0, ("high", f"Asking price is {mult:.1f}x weighted SDE, above the {val['multiples']['high']}x top of "
                                            "your range. Expect buyers and lenders to push back unless there's real estate, "
                                            "inventory or a clear growth story included."))
    elif n < val["low"] * 0.95:
        metrics["flags"].insert(0, ("low", f"Asking price is only {mult:.1f}x weighted SDE. Possibly underpriced, worth a second look."))


def risk_flags(m, addbacks):
    flags = []
    n = len(m["years"])
    last = n - 1
    if n >= 2 and m["revenue_growth"][last] is not None and m["revenue_growth"][last] < 0:
        flags.append(("high", f"Revenue fell {abs(m['revenue_growth'][last]):.1f}% in {m['years'][last]}. "
                              "Buyers will ask why, and lenders weight the latest year most."))
    if n >= 2:
        swing = m["gross_margin"][last] - m["gross_margin"][last - 1]
        if abs(swing) >= 4:
            flags.append(("medium", f"Gross margin moved {swing:+.1f} pts year over year. Expect questions on pricing or input costs."))
    routine = {"Owner compensation", "Depreciation & amortization", "Interest", "Income taxes"}
    disc = sum(a["values"][last] for a in addbacks if a["enabled"] and a["type"] not in routine)
    if m["sde"][last] > 0 and disc:
        share = disc / m["sde"][last] * 100
        if share >= 20:
            flags.append(("high" if share >= 35 else "medium",
                          f"Discretionary addbacks (beyond owner pay, D&A and interest) are {share:.0f}% of SDE. "
                          "Buyers and lenders discount these hard unless each one is documented."))
    verify = [a for a in addbacks if a["enabled"] and a["confidence"] == "verify"]
    if verify:
        amt = sum(a["values"][last] for a in verify)
        flags.append(("medium", f"{len(verify)} addback(s) worth ${amt:,.0f} in {m['years'][last]} need documentation "
                                f"({', '.join(a['name'] for a in verify)})."))
    if m["net_income"][last] < 0 < m["sde"][last]:
        flags.append(("medium", "Business shows a book loss but positive SDE. Fine if addbacks hold up, but it's the first thing a buyer will poke at."))
    if not any(a["type"] == "Owner compensation" for a in addbacks):
        flags.append(("low", "No owner salary line found. If the owner takes draws or dividends instead, note it so SDE isn't understated."))
    if m["sde_margin"][last] > 40:
        flags.append(("low", f"SDE margin is {m['sde_margin'][last]:.0f}%, unusually high for most Main Street businesses. Double-check revenue and addbacks."))
    return flags
