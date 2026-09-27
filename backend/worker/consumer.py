import os
import sys
import json
import uuid
import pika
import logging
import asyncio
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_BACKEND_DIR = Path(__file__).resolve().parents[1]  # .../backend/
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parents[2] / "infra" / ".env"
load_dotenv(dotenv_path=env_path)

from sqlalchemy import select, update, func
from sqlalchemy.orm import selectinload

from core.db.models import InteractionJob, Drug, DrugIngredient, FdaLabel, DrugClass
from worker.langgraph_pipeline.graph import run_interaction_check

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persistent event loop
# ---------------------------------------------------------------------------
# We run ONE event loop for the entire lifetime of the worker process on a
# dedicated background thread.  This avoids the Windows asyncio/asyncpg crash
# caused by asyncio.run() creating and immediately destroying a new event loop
# per message, which orphans the SQLAlchemy async engine's connection pool.
#
# The engine + session factory are created INSIDE the loop thread so they are
# always bound to the correct living event loop.
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_async_session = None  # populated inside the loop thread before first message


def _build_session_factory():
    """Create the SQLAlchemy async engine + session factory.
    Must be called from within the persistent event loop thread."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    DATABASE_URL = os.environ["DATABASE_URL"]
    engine = create_async_engine(DATABASE_URL, echo=False)
    return async_sessionmaker(engine, expire_on_commit=False)


def _loop_thread_target(loop: asyncio.AbstractEventLoop):
    """Thread target: set the loop, initialise the session factory, run forever."""
    global _async_session
    asyncio.set_event_loop(loop)
    _async_session = _build_session_factory()
    loop.run_forever()


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return (and lazily start) the persistent event loop."""
    global _loop
    if _loop is None:
        _loop = asyncio.new_event_loop()
        t = threading.Thread(target=_loop_thread_target, args=(_loop,), daemon=True)
        t.start()
        # Give the thread a moment to set up the session factory.
        time.sleep(0.3)
    return _loop


# ---------------------------------------------------------------------------
# Async helpers — always run inside the persistent loop
# ---------------------------------------------------------------------------

NOTIFY_CHANNEL = "interaction_jobs_channel"


async def fetch_drug_data(drug_id: int):
    async with _async_session() as session:
        stmt = select(Drug).options(selectinload(Drug.ingredients)).where(Drug.id == drug_id)
        result = await session.execute(stmt)
        drug = result.scalar_one_or_none()

        if not drug:
            return None

        rxcui_list = [ing.rxcui for ing in drug.ingredients if ing.rxcui]
        labels_by_rxcui = {}
        if rxcui_list:
            label_stmt = select(FdaLabel).where(FdaLabel.rxcui.in_(rxcui_list))
            label_result = await session.execute(label_stmt)
            labels = label_result.scalars().all()
            labels_by_rxcui = {label.rxcui: label for label in labels}

        ingredients_resp = []
        for ing in drug.ingredients:
            label = labels_by_rxcui.get(ing.rxcui) if ing.rxcui else None

            # Load pharmacological class data for this ingredient
            drug_classes = []
            if ing.rxcui:
                dc_stmt = select(DrugClass).where(DrugClass.rxcui == ing.rxcui)
                dc_result = await session.execute(dc_stmt)
                dc = dc_result.scalar_one_or_none()
                if dc and dc.class_names:
                    drug_classes = [
                        {"class_name": name, "class_source": source}
                        for name, source in zip(dc.class_names, dc.class_sources)
                    ]

            ingredients_resp.append({
                "generic_name": ing.generic_name,
                "dose": ing.dose,
                "rxcui": ing.rxcui,
                "drug_interactions": label.drug_interactions if label else None,
                "warnings": label.warnings if label else None,
                "boxed_warning": label.boxed_warning if label else None,
                "drug_classes": drug_classes,
            })

        return {
            "brand_name": drug.brand_name,
            "dosage_form": drug.dosage_form,
            "ingredients": ingredients_resp,
        }


async def update_job_status(job_id: str, status: str, result: dict = None, error_message: str = None):
    from sqlalchemy import text
    async with _async_session() as session:
        values = {"status": status}
        if status in ("done", "failed"):
            values["completed_at"] = func.now()
        if result is not None:
            values["result"] = result
        if error_message is not None:
            values["error_message"] = error_message

        stmt = update(InteractionJob).where(InteractionJob.id == uuid.UUID(job_id)).values(**values)
        await session.execute(stmt)

        # Issue NOTIFY in the same transaction so Postgres delivers it only
        # after commit, guaranteeing the listener sees the committed row.
        if status in ("done", "failed"):
            await session.execute(
                text("SELECT pg_notify(:channel, :payload)"),
                {"channel": NOTIFY_CHANNEL, "payload": job_id},
            )

        await session.commit()

        if status in ("done", "failed"):
            logger.info("NOTIFY %s | job_id=%s | status=%s", NOTIFY_CHANNEL, job_id, status)


async def process_job_async(job_id: str, drug_a_id: int, drug_b_id: int):
    logger.info(f"Processing job {job_id} for drug_a={drug_a_id} and drug_b={drug_b_id}")

    await update_job_status(job_id, status="processing")

    drug_a = await fetch_drug_data(drug_a_id)
    drug_b = await fetch_drug_data(drug_b_id)

    if not drug_a or not drug_b:
        error_msg = (
            f"Missing drug data in DB. "
            f"drug_a_id={drug_a_id} (found={bool(drug_a)}), "
            f"drug_b_id={drug_b_id} (found={bool(drug_b)})"
        )
        logger.error(f"Job {job_id} failed: {error_msg}")
        await update_job_status(job_id, status="failed", error_message=error_msg)
        return

    try:
        logger.info(f"Running interaction check pipeline for job {job_id}")
        result = await asyncio.to_thread(run_interaction_check, drug_a, drug_b)
        logger.info(f"Job {job_id} pipeline completed successfully.")
        await update_job_status(job_id, status="done", result=result)
    except Exception as e:
        logger.exception(f"Pipeline error for job {job_id}: {e}")
        await update_job_status(job_id, status="failed", error_message=str(e))


# ---------------------------------------------------------------------------
# Synchronous pika callback — bridges into the persistent event loop
# ---------------------------------------------------------------------------

def on_message(ch, method, properties, body):
    try:
        payload = json.loads(body)
        job_id = payload["job_id"]
        drug_a_id = payload["drug_a_id"]
        drug_b_id = payload["drug_b_id"]

        # Submit the coroutine to the persistent loop and block until it finishes.
        future = asyncio.run_coroutine_threadsafe(
            process_job_async(job_id, drug_a_id, drug_b_id),
            _get_loop(),
        )
        future.result()  # blocks the pika thread; raises on exception
    except Exception as e:
        logger.exception(f"Failed to process message {body}: {e}")
    finally:
        ch.basic_ack(delivery_tag=method.delivery_tag)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # Warm up the persistent loop and session factory before accepting messages.
    _get_loop()

    rabbitmq_url = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    logger.info(f"Connecting to RabbitMQ at {rabbitmq_url}")

    parameters = pika.URLParameters(rabbitmq_url)
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()

    channel.queue_declare(queue="interaction_checks", durable=True)
    channel.basic_qos(prefetch_count=1)

    channel.basic_consume(queue="interaction_checks", on_message_callback=on_message)

    logger.info("Worker started. Waiting for jobs on 'interaction_checks' queue...")
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        logger.info("Worker stopped by user.")
        channel.stop_consuming()
    connection.close()


if __name__ == "__main__":
    main()
