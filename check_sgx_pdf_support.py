
import importlib.util

packages = [
    "pymupdf",
    "fitz",
    "pdfplumber",
    "pypdf",
    "camelot",
    "tabula"
]

print("PDF libraries installed:\n")

for package in packages:
    try:
        installed = importlib.util.find_spec(package) is not None
    except (ImportError, ValueError):
        installed = False

    print(f"{package}: {'Available' if installed else 'Not installed'}")


print("\nChecking downloaded DBS Annual Report...")

try:
    import fitz

    pdf = fitz.open("test_sgx_document_1.pdf")

    print("PDF pages:", len(pdf))
    print("\nFirst page text preview:\n")
    print(pdf[0].get_text()[:1500])

except ImportError:
    print("PyMuPDF is not installed yet.")

except Exception as error:
    print("PDF reading error:", error)
