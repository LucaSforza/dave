"""HTTP API for the local DAVE controller."""
from __future__ import annotations

import base64
import binascii
import re
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import RedisError
from urllib.parse import quote

from .models import JobCreate, JobRecord, JobState
from .service import IdempotencyConflict, JobService
from .settings import Settings
from .store import RedisStore
from .transport import SSHTransport, TransportError


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(pause|resume|reset|checkpoint)$")


def create_app(*, store=None, transport=None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    store = store if store is not None else RedisStore(settings.redis_url)
    transport = transport if transport is not None else SSHTransport(settings)
    service = JobService(store)
    app = FastAPI(title="DAVE Cluster Control", version="0.1.0", description="Local single-user API; no cluster connection is made on import.")
    app.state.store, app.state.transport, app.state.service = store, transport, service

    def remote(operation: str, payload: dict[str, Any]):
        try:
            return transport.call(operation, payload)
        except (TransportError, OSError, TimeoutError) as exc:
            if operation == "desktop" and any(token in str(exc).lower() for token in ("not_ready", "not ready", "runtime is not ready")):
                raise HTTPException(status_code=409, detail="Desktop is not ready") from exc
            raise HTTPException(status_code=503, detail="Cluster operation unavailable") from exc

    def store_call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except RedisError as exc:
            raise HTTPException(status_code=503, detail="State store unavailable") from exc

    def find(job_id: UUID) -> JobRecord:
        record = store_call(service.get_job, job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return record

    def current_allocation(record: JobRecord, *, running_only: bool = False) -> int:
        if not record.allocations:
            raise HTTPException(status_code=409, detail="Job has no allocation")
        if running_only and (record.cancel_requested or record.state != JobState.RUNNING):
            raise HTTPException(status_code=409, detail="Job has no current running allocation")
        return record.allocations[-1].index

    @app.get("/health")
    def health():
        client = getattr(store, "client", None)
        if client is not None:
            try:
                client.ping()
            except RedisError as exc:
                raise HTTPException(status_code=503, detail="State store unavailable") from exc
        return {"status": "ok", "redis": "ok"}

    @app.get("/health/live")
    def liveness():
        return {"status": "ok"}

    @app.get("/cluster")
    def cluster():
        return remote("preflight", {})

    @app.post("/jobs", response_model=JobRecord, status_code=202)
    def create_job(request: JobCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")):
        try:
            return store_call(service.create_job, request, idempotency_key)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail="Idempotency key conflicts with a different request") from exc

    @app.get("/jobs", response_model=list[JobRecord])
    def list_jobs():
        return store_call(service.list_jobs)

    @app.get("/jobs/{job_id}", response_model=JobRecord)
    def get_job(job_id: UUID):
        return find(job_id)

    @app.post("/jobs/{job_id}/cancel", response_model=JobRecord)
    def cancel_job(job_id: UUID):
        record = store_call(service.cancel_job, job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Job not found")
        # Cancellation is durable locally first; worker owns Slurm submission/cancellation.
        return record

    @app.get("/jobs/{job_id}/logs")
    def logs(job_id: UUID, offset: int = Query(default=0, ge=0), limit: int = Query(default=65536, ge=1, le=65536)):
        record = find(job_id)
        index = current_allocation(record)
        return remote("logs", {"job_id": str(job_id), "allocation_index": index, "offset": offset, "limit": limit})

    @app.get("/jobs/{job_id}/artifacts")
    def artifacts(job_id: UUID):
        find(job_id)
        return remote("artifacts", {"job_id": str(job_id)})

    @app.get("/jobs/{job_id}/artifacts/{artifact_path:path}/download")
    def download(job_id: UUID, artifact_path: str):
        find(job_id)
        if not artifact_path or artifact_path.startswith("/") or "\\" in artifact_path or any(part in ("", ".", "..") for part in artifact_path.split("/")):
            raise HTTPException(status_code=422, detail="Invalid artifact path")
        listing = remote("artifacts", {"job_id": str(job_id)})
        listed = listing.get("artifacts", [])
        item = next((entry for entry in listed if entry.get("path") == artifact_path), None)
        if item is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        if item.get("size", 0) > 8 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Artifact exceeds 8 MiB download limit")
        result = remote("download", {"job_id": str(job_id), "path": artifact_path})
        try:
            data = base64.b64decode(result["data_base64"], validate=True)
        except (KeyError, ValueError, binascii.Error) as exc:
            raise HTTPException(status_code=503, detail="Invalid artifact response") from exc
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Artifact exceeds 8 MiB download limit")
        filename = artifact_path.rsplit("/", 1)[-1]
        safe_fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "artifact"
        disposition = f"attachment; filename=\"{safe_fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"
        return Response(data, media_type="application/octet-stream", headers={"Content-Disposition": disposition})

    @app.get("/jobs/{job_id}/desktop")
    def desktop(job_id: UUID):
        record = find(job_id)
        index = current_allocation(record, running_only=True)
        descriptor = remote("desktop", {"job_id": str(job_id), "allocation_index": index})
        node, user = descriptor.get("node"), descriptor.get("user")
        if descriptor.get("ready") is not True or not isinstance(node, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", node) or ".." in node or not isinstance(user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}", user) or not isinstance(descriptor.get("port"), int) or not 1 <= descriptor["port"] <= 65535:
            raise HTTPException(status_code=409, detail="Desktop is not ready")
        return {"ready": True, "node": descriptor["node"], "user": descriptor["user"], "port": descriptor["port"], "allocation_index": index}

    @app.post("/jobs/{job_id}/control")
    def control(job_id: UUID, request: ControlRequest):
        record = find(job_id)
        index = current_allocation(record, running_only=True)
        return remote("control", {"job_id": str(job_id), "allocation_index": index, "action": request.action})

    return app


app = create_app()
