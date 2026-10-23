from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scenario: str = Field(default="uw_lidar", pattern="^uw_lidar$")
    x: FiniteFloat = Field(default=4.0, ge=-1000, le=1000)
    y: FiniteFloat = Field(default=4.0, ge=-1000, le=1000)
    z: FiniteFloat = Field(default=-93.0, ge=-1000, le=1000)
    yaw: FiniteFloat = Field(default=-1.8, ge=-6.2832, le=6.2832)
    cpus: int = Field(default=16, ge=1, le=16)
    gpus: int = Field(default=1, ge=0, le=2)
    memory_gb: int = Field(default=32, gt=0, le=1024)
    walltime_minutes: int = Field(default=30, ge=5, le=30)
    auto_resume: bool = True
    max_allocations: int = Field(default=2, ge=1, le=4)


class JobState(StrEnum):
    QUEUED = "queued"
    SUBMITTING = "submitting"
    PENDING = "pending"
    RUNNING = "running"
    WAITING_RESUME = "waiting_resume"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"


class AllocationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=1)
    slurm_job_id: str | None = None
    state: str = "NOT_SUBMITTED"
    submission_token: str
    submitted_at: datetime | None = None
    finished_at: datetime | None = None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    state: JobState = JobState.QUEUED
    request: JobCreate
    allocations: list[AllocationRecord] = Field(default_factory=list)
    cancel_requested: bool = False
    checkpoint: dict[str, Any] | None = None
    runtime: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
    submission_token: str
    revision: int = Field(default=0, ge=0)
    last_reconciled_at: datetime | None = None
    reconciliation_error: str | None = None
