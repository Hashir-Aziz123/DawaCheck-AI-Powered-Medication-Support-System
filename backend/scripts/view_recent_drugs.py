import re
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db.session import DATABASE_URL
from core.db.models import Drug

def view_recent_drugs():
    # Convert to sync URL
    sync_url = re.sub(r"^postgresql\+asyncpg://", "postgresql://", DATABASE_URL)
    engine = create_engine(sync_url)
    
    with Session(engine) as session:
        # Fetch Panadol and Tylenol
        stmt = select(Drug).where(
            Drug.brand_name.ilike('%panadol%') | Drug.brand_name.ilike('%tylenol%')
        ).order_by(Drug.created_at.desc())
        
        drugs = session.execute(stmt).scalars().unique().all()
        
        print(f"Found {len(drugs)} matching drugs in the database:\n")
        
        for drug in drugs:
            print(f"💊 Brand Name: {drug.brand_name}")
            print(f"   DRAP Reg No: {drug.drap_reg_no}")
            print("   Ingredients:")
            for ing in drug.ingredients:
                print(f"      - {ing.generic_name} (RxCUI: {ing.rxcui})")
            print("-" * 40)

if __name__ == "__main__":
    view_recent_drugs()
