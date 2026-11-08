
import pymupdf

pdf = pymupdf.open("test_sgx_document_1.pdf")

keywords = [
    "income statement",
    "balance sheet",
    "cash flow statement",
    "statement of comprehensive income"
]

exclude = [
    "independent auditor",
    "report on the audit",
    "our opinion"
]

print("POSSIBLE FINANCIAL STATEMENT PAGES\n")

for i, page in enumerate(pdf):
    text = page.get_text()
    lower = text.lower()

    if any(word in lower for word in exclude):
        continue

    matches = [
        word for word in keywords
        if word in lower
    ]

    if matches:
        print(f"Page {i + 1}: {', '.join(matches)}")
        print("Preview:", " ".join(text.split())[:180])

print("\nScan complete.")
