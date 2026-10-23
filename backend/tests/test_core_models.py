import pytest
from pydantic import ValidationError

from dave_backend.models import JobCreate, JobRecord
from uuid import uuid4


def test_job_defaults_and_serialization():
    req = JobCreate()
    assert (req.cpus, req.gpus, req.max_allocations) == (16, 1, 2)
    record = JobRecord(id=uuid4(), request=req, submission_token="stable")
    assert JobRecord.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize("payload", [{"scenario": "other"}, {"cpus": 17}, {"walltime_minutes": 31}, {"unexpected": 1}, {"x": float("nan")}])
def test_invalid_job_parameters(payload):
    with pytest.raises(ValidationError):
        JobCreate(**payload)
