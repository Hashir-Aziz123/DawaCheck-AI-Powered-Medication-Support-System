import sys
from pathlib import Path
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
import re

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.db.session import DATABASE_URL
from core.db.models import Drug

def debug_db():
    sync_url = re.sub(r"^postgresql\+asyncpg://", "postgresql://", DATABASE_URL)
    engine = create_engine(sync_url)
    
    with Session(engine) as session:
        print("1. Checking if 'Panadol' is actually in your local database...")
        stmt = select(Drug).where(Drug.brand_name.ilike('%panadol%'))
        drugs = session.execute(stmt).scalars().all()
        if not drugs:
            print("ERROR: 'Panadol' is completely missing from the database!")
            print("   You need to run resolve_drug('Panadol', session) once with the correct spelling to cache it, before you can test typos.")
            return
            
        print(f"Found {len(drugs)} 'Panadol' entries in the database:")
        for d in drugs:
            print(f"   - {d.brand_name}")
            
        print("\n2. Asking PostgreSQL to calculate the exact trigram similarity score against search_name...")
        for d in drugs:
            sim_stmt = select(func.similarity(d.search_name, 'Panidal'))
            score = session.execute(sim_stmt).scalar()
            print(f"   Similarity between '{d.search_name}' and 'Panidal': {score}")

if __name__ == "__main__":
    debug_db()
