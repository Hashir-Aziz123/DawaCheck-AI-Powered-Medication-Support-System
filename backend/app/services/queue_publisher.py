"""
queue_publisher.py — thin wrapper for publishing interaction-check jobs to
the RabbitMQ 'interaction_checks' queue.

Single responsibility: publish. All DB work (INSERT / UPDATE) stays in the
route handler. This module never touches a database session.
"""

import json
import logging
import os
from pathlib import Path
from uuid import UUID

import pika
from dotenv import load_dotenv

# Load infra/.env so RABBITMQ_URL is available regardless of how the app is
# started (uvicorn, pytest, etc.).
load_dotenv(dotenv_path=Path(__file__).resolve().parents[3] / "infra" / ".env")

logger = logging.getLogger(__name__)

QUEUE_NAME = "interaction_checks"


def publish_interaction_check(job_id: UUID, drug_a_id: int, drug_b_id: int) -> None:
    """Publish a single interaction-check job to the RabbitMQ queue.

    Opens a fresh, short-lived BlockingConnection per call. For the expected
    low-volume request rate (one connection per API call) this is fine; a
    long-lived connection pool can be added later if throughput demands it.

    Args:
        job_id:     UUID of the pre-inserted interaction_jobs row.
        drug_a_id:  PK of the first drug in the 'drugs' table.
        drug_b_id:  PK of the second drug in the 'drugs' table.

    Raises:
        Exception: Any pika / AMQP error is propagated so the caller can
                   handle it (e.g. mark the job as failed and return 503).
    """
    rabbitmq_url = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")

    payload = json.dumps(
        {
            "job_id": str(job_id),
            "drug_a_id": drug_a_id,
            "drug_b_id": drug_b_id,
        }
    )

    parameters = pika.URLParameters(rabbitmq_url)
    connection = pika.BlockingConnection(parameters)
    try:
        channel = connection.channel()
        channel.queue_declare(queue=QUEUE_NAME, durable=True)
        channel.basic_publish(
            exchange="",
            routing_key=QUEUE_NAME,
            body=payload,
            properties=pika.BasicProperties(delivery_mode=2),  # persistent
        )
        logger.info(
            "publish_interaction_check | Published job %s → queue '%s'",
            job_id,
            QUEUE_NAME,
        )
    finally:
        connection.close()
