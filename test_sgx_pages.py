import requests

URL = "https://api.sgx.com/financialreports/v1.0"

headers = {
    "User-Agent": "AiAssistant/1.0 research"
}

pages = [2, 3, 54, 55]

for page in pages:
    params = {
        "pagestart": page,
        "pagesize": 250,
        "params": "id,companyName,documentDate,securityName,title,url"
    }

    try:
        response = requests.get(
            URL,
            params=params,
            headers=headers,
            timeout=30
        )

        response.raise_for_status()
        result = response.json()

        records = result.get("data") or []
        meta = result.get("meta", {})

        print(f"\nPage: {page}")
        print("API status:", meta.get("code"))
        print("Total pages:", meta.get("totalPages"))
        print("Records:", len(records))

        for record in records:
            if "DBS" in record.get("companyName", "").upper():
                print("DBS report:", record)

    except Exception as error:
        print(f"Page {page} error:", error)