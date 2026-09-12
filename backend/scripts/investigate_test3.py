import sys
from pathlib import Path
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
import json
from core.clients.drap_client import get_drug_detail, parse_detail_html, DRAP_URL, DRAP_HEADERS, SESSION
import psycopg2
from core.db.session import DATABASE_URL
import re

def _sync_url(url: str) -> str:
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", url)

def run():
    print("--- 1. Querying DB for 'test3' ---")
    conn = psycopg2.connect(_sync_url(DATABASE_URL))
    cur = conn.cursor()
    cur.execute("SELECT drap_reg_no, brand_name, company_name FROM drugs WHERE company_name = 'test3'")
    test3_rows = cur.fetchall()
    print(f"Found {len(test3_rows)} rows with 'test3':")
    for r in test3_rows[:5]:
        print(f"  {r}")
    
    cur.execute("SELECT drap_reg_no, brand_name, company_name FROM drugs WHERE company_name IS NULL or company_name = ''")
    null_rows = cur.fetchall()
    print(f"\nFound {len(null_rows)} rows with NULL/empty company_name:")
    for r in null_rows:
        print(f"  {r}")
    
    print("\n--- 2. Fetching live DRAP detail for Aspirin (002212) ---")
    resp = SESSION.post(DRAP_URL, data={"webRegNo": "002212"}, headers=DRAP_HEADERS, timeout=10)
    print(f"HTTP Status: {resp.status_code}")
    
    html = resp.text
    print(f"Raw HTML contains 'test3'? {'test3' in html.lower()}")
    
    record = parse_detail_html(html)
    print("\nParsed dict:")
    print(json.dumps(record, indent=2))
    
    print("\n--- 3. Fetching live DRAP detail for Doxycycline (015093 - one of the nulls) ---")
    # Actually let's use the first null row's reg_no
    if null_rows:
        reg_no = null_rows[0][0]
        resp2 = SESSION.post(DRAP_URL, data={"webRegNo": reg_no}, headers=DRAP_HEADERS, timeout=10)
        record2 = parse_detail_html(resp2.text)
        print(f"Parsed dict for {reg_no}:")
        print(json.dumps(record2, indent=2))

if __name__ == "__main__":
    run()
