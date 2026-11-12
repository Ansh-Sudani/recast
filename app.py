import os
from pathlib import Path

from flask import Flask, render_template, request

import ai
import engine

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB
SAMPLE = Path(__file__).parent / "sample_data" / "lakeside_cafe_pnl.csv"
SAMPLE_BIZ = {
    "name": "Lakeside Café (sample)",
    "industry": "Café / coffee shop",
    "location": "Kingston, ON",
    "asking": "$395,000",
    "notes": ("Sample business for demo purposes only. Owner-operated café near a university campus, "
              "open 7 days. Owner works ~45 hrs/week on the floor and does the books. 6 part-time staff. "
              "Lease has 4 years left with one 5-year renewal option. Sells catering trays to two local "
              "offices, which are about 15% of sales. Owner's spouse is on payroll part-time doing social media. "
              "Espresso machine replaced in 2024. Owner is retiring."),
}


@app.get("/")
def index():
    return render_template("index.html", has_key=bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GROQ_API_KEY")))


def _run(filename, raw, biz, low, mid, high):
    pnl = engine.parse_pnl(filename, raw)
    metrics, addbacks = engine.compute(pnl)
    val = engine.valuation(metrics["weighted_sde"], low, mid, high)
    engine.asking_flag(biz.get("asking"), metrics, val)
    ask = engine._num(__import__("re").sub(r"[^\d.]", "", biz.get("asking") or ""))
    biz = {**biz, "_asking_mult": f"{ask / metrics['weighted_sde']:.1f}x" if ask and metrics["weighted_sde"] > 0 else "",
           "_addbacks": ", ".join(f"{a['name']} ({int(a['share']*100)}%)" for a in addbacks if a["enabled"])}
    cim, source = ai.draft_cim(biz, metrics, val)
    return render_template("report.html", biz=biz, m=metrics, addbacks=addbacks,
                           val=val, cim=cim, cim_source=source, lines=pnl["lines"])


def _f(name, default):
    try:
        return float(request.form.get(name) or default)
    except ValueError:
        return default


@app.post("/analyze")
def analyze():
    f = request.files.get("pnl")
    if not f or not f.filename:
        return render_template("index.html", error="Upload a P&L file (CSV or Excel)."), 400
    biz = {k: (request.form.get(k) or "").strip() for k in ("name", "industry", "location", "asking", "notes")}
    try:
        return _run(f.filename, f.read(), biz, _f("m_low", 2.0), _f("m_mid", 2.5), _f("m_high", 3.0))
    except ValueError as e:
        return render_template("index.html", error=str(e)), 400


@app.get("/sample")
def sample():
    return _run(SAMPLE.name, SAMPLE.read_bytes(), SAMPLE_BIZ, 2.0, 2.5, 3.0)


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
