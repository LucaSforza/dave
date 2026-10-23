"""Synchronous Redis persistence with atomic records and controller lease."""
import uuid
from typing import Any

from redis.exceptions import WatchError

from .models import JobRecord

_LOCK_SCRIPT = """
if redis.call('exists', KEYS[1]) == 0 then
 redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[2]); return 1
end
return 0
"""
_RENEW_SCRIPT = "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('expire',KEYS[1],ARGV[2]) else return 0 end"
_UNLOCK_SCRIPT = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class RedisStore:
    def __init__(self, redis_url: str, client: Any = None, *, lock_seconds: int = 30):
        if client is None:
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError("Install redis to use RedisStore") from exc
            client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)
        self.client = client
        self.lock_seconds = lock_seconds

    @staticmethod
    def _key(job_id: str) -> str:
        return f"dave:job:{job_id}"

    def get(self, job_id: str) -> JobRecord | None:
        raw = self.client.get(self._key(str(job_id)))
        return JobRecord.model_validate_json(raw) if raw else None

    def list(self) -> list[JobRecord]:
        keys = list(self.client.scan_iter(match="dave:job:*"))
        records = [self.get(str(k).rsplit(":", 1)[-1]) for k in keys]
        return sorted((r for r in records if r is not None), key=lambda r: r.created_at, reverse=True)

    def create(self, record: JobRecord) -> bool:
        return bool(self.client.set(self._key(str(record.id)), record.model_dump_json(), nx=True))

    def save(self, record: JobRecord, expected_revision: int | None = None, lock_token: str | None = None) -> bool:
        key = self._key(str(record.id))
        # Decode only for comparisons: Redis cjson re-encodes [] as {}, which
        # corrupts empty allocations and nested checkpoint arrays on real Redis.
        script = """
local old=redis.call('get',KEYS[1]); if not old then return 0 end
local o=cjson.decode(old); local n=cjson.decode(ARGV[1]);
if tonumber(o.revision or 0) ~= tonumber(ARGV[2]) then return -1 end
if ARGV[3] ~= '' and redis.call('get',KEYS[2]) ~= ARGV[3] then return -2 end
if o.cancel_requested and not n.cancel_requested then return -1 end
redis.call('set',KEYS[1],ARGV[1]); return 1
"""
        expected = record.revision if expected_revision is None else expected_revision
        serialized = record.model_copy(update={"revision": expected + 1}).model_dump_json()
        result = self.client.eval(script, 2, key, "dave:controller:lock", serialized, expected, lock_token or "")
        if result == 1:
            record.revision = expected + 1
            return True
        return False

    def request_cancel(self, job_id: str) -> JobRecord | None:
        from .models import JobState, now_utc

        key = self._key(str(job_id))
        for _ in range(20):
            with self.client.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    if raw is None:
                        return None
                    record = JobRecord.model_validate_json(raw)
                    if record.cancel_requested:
                        return record
                    record.cancel_requested = True
                    record.updated_at = now_utc()
                    if not record.allocations or record.allocations[-1].state == "NOT_SUBMITTED":
                        record.state = JobState.CANCELLED
                    record.revision += 1
                    pipe.multi()
                    pipe.set(key, record.model_dump_json())
                    pipe.execute()
                    return record
                except WatchError:
                    continue
        raise RuntimeError("Cancellation raced with repeated updates; retry the request")

    def create_idempotent(self, record: JobRecord, idempotency_key: str) -> tuple[JobRecord, bool]:
        key = "dave:idempotency:" + idempotency_key
        script = """
local old=redis.call('get',KEYS[1]); if old then return old end
redis.call('set',KEYS[1],ARGV[1]); redis.call('set',ARGV[2],ARGV[3]); return ARGV[1]
"""
        result = self.client.eval(script, 1, key, str(record.id), self._key(str(record.id)), record.model_dump_json())
        existing = self.get(result.decode() if isinstance(result, bytes) else str(result))
        return existing or record, existing is not None and existing.id == record.id

    def acquire_lock(self, token: str | None = None) -> str | None:
        token = token or str(uuid.uuid4())
        if self.client.eval(_LOCK_SCRIPT, 1, "dave:controller:lock", token, self.lock_seconds):
            return token
        return None

    def renew_lock(self, token: str) -> bool:
        return bool(self.client.eval(_RENEW_SCRIPT, 1, "dave:controller:lock", token, self.lock_seconds))

    def release_lock(self, token: str) -> None:
        self.client.eval(_UNLOCK_SCRIPT, 1, "dave:controller:lock", token)
