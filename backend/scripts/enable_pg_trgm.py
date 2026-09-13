import re
import sys
from pathlib import Path
from sqlalchemy import text, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db.session import DATABASE_URL

def enable_pg_trgm():
    # Convert async connection string to sync for the setup script
    sync_url = re.sub(r"^postgresql\+asyncpg://", "postgresql://", DATABASE_URL)
    engine = create_engine(sync_url)
    
    with engine.connect() as conn:
        print("Enabling pg_trgm extension...")
        # Creating extensions usually requires autocommit
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))
        print("Success! pg_trgm is permanently enabled in the database.")

if __name__ == "__main__":
    enable_pg_trgm()
