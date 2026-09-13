import sys
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import re

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db.session import DATABASE_URL
from app.services.drug_resolution import resolve_drug

def test_fuzzy():
    # Convert async connection string to sync for the manual test script
    sync_url = re.sub(r"^postgresql\+asyncpg://", "postgresql://", DATABASE_URL)
    engine = create_engine(sync_url)
    
    with Session(engine) as session:
        print("--- Testing resolve_drug('Panidal') ---")
        print("Expecting fuzzy DB hit for Panadol if Panadol is in the DB.")
        print("Make sure you've run 'python scripts/enable_pg_trgm.py' first!")
        print("-" * 40)
        
        result = resolve_drug("Panidal", session)
        
        print("\n--- Result ---")
        print(f"Status:     {result.status}")
        print(f"Source:     {result.source}")
        print(f"Matched As: {result.matched_as}")
        print(f"Brand Name: {result.brand_name}")
        
if __name__ == "__main__":
    test_fuzzy()
