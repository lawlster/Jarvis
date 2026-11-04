
import requests
from pathlib import Path
from datetime import datetime

MODEL = "qwen3:8b"
OLLAMA_URL = "http://localhost:11434/api/chat"

# Project reports directory
BASE_DIR = Path(__file__).resolve().parent.parent
REPORTS_DIR = BASE_DIR / "reports"


def generate_investment_report(research_data):

    ticker = research_data.get("ticker", "UNKNOWN")

    prompt = f"""
You are a professional investment research analyst.

Analyse the following company research data.

COMPANY RESEARCH DATA:

{research_data}

Prepare a structured investment research report.

Use the following sections:

1. COMPANY OVERVIEW
Explain what the company does and how it generates revenue.

2. FINANCIAL PERFORMANCE
Analyse:
- Revenue
- Operating income
- Net income
- Free cash flow
- Debt
- Cash position

3. BUSINESS QUALITY
Evaluate:
- Competitive advantages
- Potential economic moat
- Industry position
- Business model durability

Clearly identify missing information.

4. FINANCIAL STRENGTH
Evaluate profitability, cash generation and balance sheet strength.

5. RISKS
Identify important business, financial and industry risks.

6. VALUATION
Discuss available valuation metrics.
Do not invent an intrinsic value.
If DCF assumptions are unavailable, explain what is needed.

7. INVESTMENT THESIS
Present:
- Bull case
- Base case
- Bear case

8. FINAL ASSESSMENT
Summarise the company's strengths, weaknesses and uncertainties.

IMPORTANT RULES:

- Do not invent financial figures.
- Distinguish facts from assumptions.
- Do not treat search snippets as verified facts.
- Do not claim to have read company filings unless supplied.
- Identify missing information.
- Explain uncertainty.
- Do not automatically recommend buying or selling.
"""

    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "stream": False
            },
            timeout=300
        )

        response.raise_for_status()

        result = response.json()
        report = result["message"]["content"]

        # Create reports folder if it doesn't exist
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

        # Generate a unique filename
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"{ticker}_{timestamp}_investment_report.txt"

        report_path = REPORTS_DIR / filename

        # Save report
        report_path.write_text(
            report,
            encoding="utf-8"
        )

        print(f"\nInvestment report saved successfully!")
        print(f"Location: {report_path}")

        return report

    except Exception as error:
        return f"Report generation error: {error}"
