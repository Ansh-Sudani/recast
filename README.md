# Recast

Upload a seller's P&L and get recast financials (SDE, EBITDA), addbacks traced to each line item, the red flags a buyer will raise, an indicative value range, and a first-draft CIM. Built for Canadian business brokers.

## Run locally

```bash
pip install -r requirements.txt
export GROQ_API_KEY=gsk_...           # free at console.groq.com (or ANTHROPIC_API_KEY); without a key the CIM uses template text
python app.py                          # http://localhost:5000
```

Open `/sample` for the demo café report.

## How it works

- `engine.py`: parses CSV/XLSX, sorts lines into revenue/COGS/expense, finds addbacks by rule, computes SDE, weighted SDE (3-2-1) and flags. No AI here, so every number traces back to a line in the file.
- `ai.py`: Claude writes the narrative sections from those numbers. It's told not to invent figures and to list missing info instead.
- `templates/report.html`: the broker edits addback % and multiples, and the report recalculates live. "Save as PDF" prints a clean copy.

Uploads are never written to disk.

## Deploy

Render or Railway: new web service, start command `gunicorn app:app`, set `GROQ_API_KEY` (free) or `ANTHROPIC_API_KEY`.
Vercel: `vercel.json` is included (Python runtime).

## Input format

`Line Item, Category, FY2023, FY2024, FY2025`. Category is Revenue / COGS / Expense / Other Income. Oldest year on the left. Template: `static/template_pnl.csv`.
