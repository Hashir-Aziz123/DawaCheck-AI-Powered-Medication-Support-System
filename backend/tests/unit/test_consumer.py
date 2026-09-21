import pytest
import json
import uuid
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

from worker.consumer import on_message, process_job_async

@pytest.mark.asyncio
@patch("worker.consumer.update_job_status", new_callable=AsyncMock)
@patch("worker.consumer.fetch_drug_data", new_callable=AsyncMock)
@patch("worker.consumer.run_interaction_check")
async def test_process_job_async_success(mock_run_interaction_check, mock_fetch_drug_data, mock_update_job_status):
    job_id = str(uuid.uuid4())
    drug_a_id = 1
    drug_b_id = 2
    
    mock_fetch_drug_data.side_effect = [
        {"brand_name": "Drug A", "dosage_form": "tablet", "ingredients": []},
        {"brand_name": "Drug B", "dosage_form": "tablet", "ingredients": []}
    ]
    
    mock_run_interaction_check.return_value = {"final_status": "interaction_found"}
    
    await process_job_async(job_id, drug_a_id, drug_b_id)
    
    # Check status updated to processing
    mock_update_job_status.assert_any_call(job_id, status="processing")
    
    # Check fetch called twice
    assert mock_fetch_drug_data.call_count == 2
    
    # Check run_interaction_check called
    mock_run_interaction_check.assert_called_once()
    
    # Check status updated to done with result
    mock_update_job_status.assert_called_with(job_id, status="done", result={"final_status": "interaction_found"})


@pytest.mark.asyncio
@patch("worker.consumer.update_job_status", new_callable=AsyncMock)
@patch("worker.consumer.fetch_drug_data", new_callable=AsyncMock)
async def test_process_job_async_missing_drug(mock_fetch_drug_data, mock_update_job_status):
    job_id = str(uuid.uuid4())
    
    mock_fetch_drug_data.side_effect = [
        {"brand_name": "Drug A", "dosage_form": "tablet", "ingredients": []},
        None # Drug B missing
    ]
    
    await process_job_async(job_id, 1, 2)
    
    mock_update_job_status.assert_called_with(job_id, status="failed", error_message="Missing drug data in DB. drug_a_id=1 (found=True), drug_b_id=2 (found=False)")


@patch("worker.consumer.process_job_async")
@patch("worker.consumer.asyncio.run")
def test_on_message(mock_asyncio_run, mock_process_job_async):
    mock_channel = MagicMock()
    mock_method = MagicMock()
    mock_method.delivery_tag = 1
    
    job_id = str(uuid.uuid4())
    payload = {
        "job_id": job_id,
        "drug_a_id": 10,
        "drug_b_id": 20
    }
    
    on_message(mock_channel, mock_method, None, json.dumps(payload))
    
    # Ensure asyncio.run was called
    mock_asyncio_run.assert_called_once()
    
    # Ensure ack was called
    mock_channel.basic_ack.assert_called_once_with(delivery_tag=1)
