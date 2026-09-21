"""
publish_test_message.py — manually push a single job message onto the
'interaction_checks' RabbitMQ queue for end-to-end worker testing.

This script mirrors what the future /check endpoint will do:
  1. INSERT a row into interaction_jobs (status='queued')
  2. Publish the job_id + drug IDs to the queue

The worker then picks it up, updates the row to 'processing', runs the
pipeline, and finally writes 'done' + result back to the same row.

Usage (from the project root, with your venv activated):
    python backend/scripts/publish_test_message.py

Edit DRUG_A_ID / DRUG_B_ID below to match real rows in your drugs table.
"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import pika
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Path bootstrap — ensures 'core.*' resolves from backend/
# ---------------------------------------------------------------------------
_BACKEND_DIR = Path(__file__).resolve().parents[1]  # .../backend/
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ---------------------------------------------------------------------------
# Load env vars from infra/.env
# ---------------------------------------------------------------------------
env_path = Path(__file__).resolve().parents[2] / "infra" / ".env"
load_dotenv(dotenv_path=env_path)

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")

# ---------------------------------------------------------------------------
# Edit these two IDs to match real rows in your 'drugs' table.
# Run: docker exec -it drug_assistant_db psql -U dev -d drug_assistant -c "SELECT id, brand_name FROM drugs LIMIT 10;"
# ---------------------------------------------------------------------------
DRUG_A_ID = 1
DRUG_B_ID = 2


# ---------------------------------------------------------------------------
# Step 1: Pre-insert the interaction_jobs row (status = 'queued')
# The worker expects this row to already exist before it processes the message.
# ---------------------------------------------------------------------------
async def insert_job(job_id: str, drug_a_id: int, drug_b_id: int):
    from sqlalchemy import text
    from core.db.session import async_session

    async with async_session() as session:
        await session.execute(
            text(
                "INSERT INTO interaction_jobs (id, drug_a_id, drug_b_id, status) "
                "VALUES (:id, :drug_a_id, :drug_b_id, 'queued')"
            ),
            {"id": job_id, "drug_a_id": drug_a_id, "drug_b_id": drug_b_id},
        )
        await session.commit()


job_id = str(uuid.uuid4())

print(f"Inserting interaction_jobs row for job {job_id} ...")
asyncio.run(insert_job(job_id, DRUG_A_ID, DRUG_B_ID))
print("  Row inserted with status='queued'")

# ---------------------------------------------------------------------------
# Step 2: Publish the message to RabbitMQ
# ---------------------------------------------------------------------------
payload = {
    "job_id": job_id,
    "drug_a_id": DRUG_A_ID,
    "drug_b_id": DRUG_B_ID,
}

print(f"\nConnecting to RabbitMQ at {RABBITMQ_URL} ...")
connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
channel = connection.channel()
channel.queue_declare(queue="interaction_checks", durable=True)

channel.basic_publish(
    exchange="",
    routing_key="interaction_checks",
    body=json.dumps(payload),
    properties=pika.BasicProperties(delivery_mode=2),  # persistent message
)

print(f"Published job {job_id} to 'interaction_checks'")
print(f"  drug_a_id = {DRUG_A_ID}")
print(f"  drug_b_id = {DRUG_B_ID}")
print("\nNow watch the worker terminal — it will pick this up and process it.")
connection.close()

