import sys
from pathlib import Path
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
import re

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db.session import DATABASE_URL
from core.db.models import Drug
from core.generic_resolution import generate_search_name

def run_migration():
    sync_url = re.sub(r"^postgresql\+asyncpg://", "postgresql://", DATABASE_URL)
    engine = create_engine(sync_url)
    
    with engine.connect() as conn:
        print("1. Adding search_name column to drugs table...")
        try:
            conn.execute(text("ALTER TABLE drugs ADD COLUMN search_name TEXT;"))
            conn.commit()
            print("   Column added successfully.")
        except Exception as e:
            if 'already exists' in str(e).lower():
                print("   Column already exists. Skipping.")
                conn.rollback()
            else:
                raise e

    with Session(engine) as session:
        print("\n2. Backfilling existing drugs with search_name...")
        drugs = session.execute(select(Drug)).scalars().all()
        updated = 0
        for drug in drugs:
            new_name = generate_search_name(drug.brand_name)
            if drug.search_name != new_name:
                print(f"   - Updating '{drug.brand_name}' -> '{new_name}'")
                drug.search_name = new_name
                updated += 1
        
        session.commit()
        print(f"   Updated {updated} out of {len(drugs)} drugs.")

    with engine.connect() as conn:
        print("\n3. Creating pg_trgm GIN index on search_name...")
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_drugs_search_name_trgm ON drugs USING gin (search_name gin_trgm_ops);"))
        print("   Index created successfully.")
        
    print("\nMigration complete!")

if __name__ == "__main__":
    run_migration()
