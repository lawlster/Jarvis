
import requests
from urllib.parse import urljoin

ANNOUNCEMENT_URL = (
    "https://links.sgx.com/1.0.0/corporate-announcements/"
    "U6RBLH1JFNDV1QZT/"
    "4d07e8c30959620ceca55f7e574d69cf8270329fdcd719a5a2992624f3474f80"
)

headers = {
    "User-Agent": "AiAssistant/1.0 research"
}

response = requests.get(
    ANNOUNCEMENT_URL,
    headers=headers,
    timeout=60
)

response.raise_for_status()

from html.parser import HTMLParser

class PDFLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.pdf_links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        href = attrs.get("href", "")

        if href.lower().endswith(".pdf"):
            self.pdf_links.append(href)


parser = PDFLinkParser()
parser.feed(response.text)

print("PDF links found:", len(parser.pdf_links))

for index, link in enumerate(parser.pdf_links, start=1):
    pdf_url = urljoin(ANNOUNCEMENT_URL, link)

    print(f"\nDownloading document {index}")
    print("URL:", pdf_url)

    pdf_response = requests.get(
        pdf_url,
        headers=headers,
        timeout=120
    )

    print("HTTP status:", pdf_response.status_code)
    print("Content type:", pdf_response.headers.get("Content-Type"))
    print("File size:", len(pdf_response.content))

    if pdf_response.status_code == 200 and pdf_response.content.startswith(b"%PDF"):
        filename = f"test_sgx_document_{index}.pdf"

        with open(filename, "wb") as file:
            file.write(pdf_response.content)

        print("SUCCESS: PDF downloaded:", filename)
    else:
        print("FAILED: Response is not a valid PDF.")
