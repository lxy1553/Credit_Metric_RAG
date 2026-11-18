"""Shared short-lived task state and encrypted result storage for Redis."""

import json

from credit.task_payloads import TaskPayloadCipher


class RedisTaskStore:
    """Owner-scoped Redis hash repository with atomic status transitions.

    Questions and results are encrypted before reaching Redis. The hash contains only
    an owner hash, status, safe metadata, and authenticated ciphertext.
    """

    STATUSES = {"queued", "running", "completed", "failed", "interrupted", "cancelled"}
    _CREATE = """
    if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
    redis.call('HSET', KEYS[1], 'owner_hash', ARGV[1], 'status', 'queued',
        'metadata', ARGV[2], 'payload', ARGV[3])
    redis.call('EXPIRE', KEYS[1], ARGV[4])
    return 1
    """
    _CREATE_AND_ENQUEUE = """
    local idempotency_key = KEYS[3]
    if idempotency_key ~= '' then
        local existing = redis.call('GET', idempotency_key)
        if existing then
            local existing_id, existing_fingerprint = string.match(existing, '^(.*)|([^|]+)$')
            if existing_fingerprint ~= ARGV[6] then return {-1} end
            return {0, existing_id}
        end
    end
    if redis.call('EXISTS', KEYS[1]) == 1 then return {-2} end
    if redis.call('XLEN', KEYS[2]) >= tonumber(ARGV[7]) then return {-3} end
    redis.call('HSET', KEYS[1], 'owner_hash', ARGV[1], 'status', 'queued',
        'metadata', ARGV[2], 'payload', ARGV[3])
    redis.call('EXPIRE', KEYS[1], ARGV[4])
    redis.call('XADD', KEYS[2], '*', 'request_id', ARGV[5], 'payload', ARGV[3])
    if idempotency_key ~= '' then
        redis.call('HSET', KEYS[1], 'idempotency_key', idempotency_key)
        redis.call('SET', idempotency_key, ARGV[5] .. '|' .. ARGV[6], 'EX', ARGV[4])
    end
    return {1, ARGV[5]}
    """
    _CLAIM = """
    local status = redis.call('HGET', KEYS[1], 'status')
    local lease_until = tonumber(redis.call('HGET', KEYS[1], 'lease_until_ms') or '0')
    local now = redis.call('TIME')
    now = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
    if status ~= 'queued' and not (status == 'running' and lease_until <= now) then return false end
    redis.call('HSET', KEYS[1], 'status', 'running', 'lease_owner', ARGV[1],
        'lease_until_ms', now + tonumber(ARGV[2]))
    return redis.call('HGET', KEYS[1], 'payload')
    """
    _HEARTBEAT = """
    if redis.call('HGET', KEYS[1], 'status') ~= 'running'
        or redis.call('HGET', KEYS[1], 'lease_owner') ~= ARGV[1] then return 0 end
    local now = redis.call('TIME')
    now = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
    local lease_until = tonumber(redis.call('HGET', KEYS[1], 'lease_until_ms') or '0')
    if lease_until <= now then return 0 end
    redis.call('HSET', KEYS[1], 'lease_until_ms', now + tonumber(ARGV[2]))
    return 1
    """
    _UPDATE_METADATA = """
    if redis.call('HGET', KEYS[1], 'status') ~= 'running'
        or redis.call('HGET', KEYS[1], 'lease_owner') ~= ARGV[1] then return 0 end
    local now = redis.call('TIME')
    now = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
    local lease_until = tonumber(redis.call('HGET', KEYS[1], 'lease_until_ms') or '0')
    if lease_until <= now then return 0 end
    redis.call('HSET', KEYS[1], 'metadata', ARGV[2])
    return 1
    """
    _TOUCH = """
    if redis.call('HGET', KEYS[1], 'owner_hash') ~= ARGV[1] then return 0 end
    redis.call('HSET', KEYS[1], 'metadata', ARGV[2])
    redis.call('EXPIRE', KEYS[1], ARGV[3])
    local idempotency_key = redis.call('HGET', KEYS[1], 'idempotency_key')
    if idempotency_key then redis.call('EXPIRE', idempotency_key, ARGV[3]) end
    return 1
    """
    _FAIL = """
    if redis.call('HGET', KEYS[1], 'status') ~= 'running'
        or redis.call('HGET', KEYS[1], 'lease_owner') ~= ARGV[1] then return 0 end
    local now = redis.call('TIME')
    now = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
    local lease_until = tonumber(redis.call('HGET', KEYS[1], 'lease_until_ms') or '0')
    if lease_until <= now then return 0 end
    redis.call('HSET', KEYS[1], 'status', 'failed', 'failure_code', ARGV[2])
    redis.call('HDEL', KEYS[1], 'payload', 'lease_owner', 'lease_until_ms')
    return 1
    """
    _CANCEL = """
    local status = redis.call('HGET', KEYS[1], 'status')
    if status ~= 'queued' and status ~= 'running' and status ~= 'completed'
        and status ~= 'failed' and status ~= 'interrupted' then return 0 end
    redis.call('HSET', KEYS[1], 'status', 'cancelled')
    redis.call('HDEL', KEYS[1], 'payload', 'lease_owner', 'lease_until_ms')
    redis.call('DEL', KEYS[2])
    return 1
    """
    _RESUME = """
    local status = redis.call('HGET', KEYS[1], 'status')
    if status ~= 'failed' and status ~= 'interrupted' then return 0 end
    if redis.call('XLEN', KEYS[2]) >= tonumber(ARGV[5]) then return -1 end
    redis.call('HSET', KEYS[1], 'status', 'queued', 'payload', ARGV[1],
        'metadata', ARGV[2])
    redis.call('HDEL', KEYS[1], 'failure_code')
    redis.call('DEL', KEYS[3])
    redis.call('EXPIRE', KEYS[1], ARGV[3])
    redis.call('XADD', KEYS[2], '*', 'request_id', ARGV[4], 'payload', ARGV[1])
    return 1
    """
    _TRANSITION = """
    if redis.call('HGET', KEYS[1], 'status') ~= ARGV[1] then return 0 end
    redis.call('HSET', KEYS[1], 'status', ARGV[2])
    redis.call('HDEL', KEYS[1], 'lease_owner', 'lease_until_ms')
    if ARGV[2] ~= 'completed' then redis.call('DEL', KEYS[2]) end
    return 1
    """
    _COMPLETE = """
    if redis.call('HGET', KEYS[1], 'status') ~= 'running'
        or redis.call('HGET', KEYS[1], 'lease_owner') ~= ARGV[1] then return 0 end
    local now = redis.call('TIME')
    now = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
    local lease_until = tonumber(redis.call('HGET', KEYS[1], 'lease_until_ms') or '0')
    if lease_until <= now then return 0 end
    redis.call('HSET', KEYS[1], 'status', 'completed')
    redis.call('HDEL', KEYS[1], 'payload', 'lease_owner', 'lease_until_ms')
    redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
    return 1
    """

    def __init__(self, client, cipher: TaskPayloadCipher, *, key_prefix="credit:question-task"):
        if not key_prefix or key_prefix.endswith(":"):
            raise ValueError("key_prefix must be non-empty and must not end with a colon")
        self.client = client
        self.cipher = cipher
        self.key_prefix = key_prefix

    def redis_key(self, request_id: str) -> str:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("request_id must be a non-empty string of at most 128 characters")
        return f"{self.key_prefix}:{request_id}"

    def result_key(self, request_id: str) -> str:
        return f"{self.redis_key(request_id)}:result"

    @staticmethod
    def _validate_owner(owner_hash: str) -> None:
        if not isinstance(owner_hash, str) or not owner_hash or len(owner_hash) > 128:
            raise ValueError("owner_hash must be a non-empty opaque identifier")

    @staticmethod
    def _validate_ttl(ttl_seconds: int) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 1:
            raise ValueError("ttl_seconds must be a positive integer")

    def create(self, request_id: str, *, owner_hash: str, metadata: dict,
               payload: str, ttl_seconds: int) -> bool:
        self._validate_owner(owner_hash)
        self._validate_ttl(ttl_seconds)
        metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        encrypted = self.cipher.encrypt(payload, request_id=request_id)
        created = self.client.eval(self._CREATE, 1, self.redis_key(request_id), owner_hash,
                                   metadata_json, encrypted, ttl_seconds)
        return bool(created)

    def create_and_enqueue(self, request_id: str, *, stream: str, idempotency_key: str | None,
                           owner_hash: str, metadata: dict, payload: str,
                           fingerprint: str, ttl_seconds: int, max_outstanding: int = 1000) -> dict:
        """Atomically register metadata, encrypted payload, idempotency, and stream entry."""
        self._validate_owner(owner_hash)
        self._validate_ttl(ttl_seconds)
        self._validate_ttl(max_outstanding)
        if not stream:
            raise ValueError("stream must be non-empty")
        if (not isinstance(fingerprint, str) or len(fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in fingerprint)):
            raise ValueError("fingerprint must be a lowercase SHA-256 hex digest")
        metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        encrypted = self.cipher.encrypt(payload, request_id=request_id)
        response = self.client.eval(
            self._CREATE_AND_ENQUEUE, 3, self.redis_key(request_id), stream,
            idempotency_key or "", owner_hash, metadata_json, encrypted, ttl_seconds,
            request_id, fingerprint, max_outstanding,
        )
        status = int(response[0])
        if status == -1:
            return {"conflict": True}
        if status == -2:
            return {"exists": True}
        if status == -3:
            return {"queue_full": True}
        actual_request_id = self._text(response[1])
        return {"created": status == 1, "request_id": actual_request_id}

    def get(self, request_id: str, *, owner_hash: str) -> dict | None:
        self._validate_owner(owner_hash)
        record = self.client.hgetall(self.redis_key(request_id))
        if not record or record.get("owner_hash") != owner_hash:
            return None
        state = {
            "request_id": request_id,
            "status": record["status"],
            "metadata": json.loads(record["metadata"]),
        }
        return state

    def touch(self, request_id: str, *, owner_hash: str, metadata: dict, ttl_seconds: int) -> bool:
        self._validate_owner(owner_hash)
        self._validate_ttl(ttl_seconds)
        value = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        touched = self.client.eval(self._TOUCH, 1, self.redis_key(request_id),
                                  owner_hash, value, ttl_seconds)
        return bool(touched)

    def claim(self, request_id: str, *, worker_id: str, lease_ms: int = 30_000) -> dict | None:
        self._validate_worker(worker_id)
        self._validate_lease(lease_ms)
        key = self.redis_key(request_id)
        encrypted = self.client.eval(self._CLAIM, 1, key, worker_id, lease_ms)
        if not encrypted:
            return None
        return {"request_id": request_id,
                "payload": self.cipher.decrypt(encrypted, request_id=request_id)}

    def heartbeat(self, request_id: str, *, worker_id: str, lease_ms: int = 30_000) -> bool:
        self._validate_worker(worker_id)
        self._validate_lease(lease_ms)
        return bool(self.client.eval(self._HEARTBEAT, 1, self.redis_key(request_id), worker_id, lease_ms))

    def transition(self, request_id: str, *, expected: str, target: str) -> bool:
        if expected not in self.STATUSES or target not in self.STATUSES:
            raise ValueError("unknown task status")
        changed = self.client.eval(self._TRANSITION, 2, self.redis_key(request_id),
                                   self.result_key(request_id), expected, target)
        return bool(changed)

    def update_metadata(self, request_id: str, *, worker_id: str, metadata: dict) -> bool:
        self._validate_worker(worker_id)
        value = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        changed = self.client.eval(self._UPDATE_METADATA, 1, self.redis_key(request_id), worker_id, value)
        return bool(changed)

    def fail(self, request_id: str, *, worker_id: str, failure_code: str) -> bool:
        self._validate_worker(worker_id)
        if not failure_code or len(failure_code) > 64:
            raise ValueError("failure_code must be a non-empty short code")
        return bool(self.client.eval(self._FAIL, 1, self.redis_key(request_id), worker_id, failure_code))

    def cancel(self, request_id: str) -> bool:
        return bool(self.client.eval(self._CANCEL, 2, self.redis_key(request_id),
                                     self.result_key(request_id)))

    def resume_and_enqueue(self, request_id: str, *, stream: str, metadata: dict,
                           payload: str, ttl_seconds: int, max_outstanding: int = 1000) -> bool | None:
        self._validate_ttl(ttl_seconds)
        self._validate_ttl(max_outstanding)
        if not stream:
            raise ValueError("stream must be non-empty")
        encrypted = self.cipher.encrypt(payload, request_id=request_id)
        metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        resumed = self.client.eval(self._RESUME, 3, self.redis_key(request_id), stream,
                                   self.result_key(request_id),
                                   encrypted, metadata_json, ttl_seconds, request_id, max_outstanding)
        return None if int(resumed) == -1 else bool(resumed)

    def complete(self, request_id: str, *, worker_id: str, result_json: str, ttl_seconds: int) -> bool:
        self._validate_worker(worker_id)
        self._validate_ttl(ttl_seconds)
        encrypted = self.cipher.encrypt(result_json, request_id=request_id)
        changed = self.client.eval(self._COMPLETE, 2, self.redis_key(request_id),
                                   self.result_key(request_id),
                                   worker_id, encrypted, ttl_seconds)
        return bool(changed)

    def get_result(self, request_id: str, *, owner_hash: str) -> str | None:
        self._validate_owner(owner_hash)
        key = self.redis_key(request_id)
        record = self.client.hmget(key, "owner_hash", "status")
        if record[0] != owner_hash or record[1] != "completed":
            return None
        encrypted = self.client.get(self.result_key(request_id))
        if not encrypted:
            return None
        return self.cipher.decrypt(encrypted, request_id=request_id)

    @staticmethod
    def _validate_worker(worker_id: str) -> None:
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be a non-empty identifier")

    @staticmethod
    def _validate_lease(lease_ms: int) -> None:
        if isinstance(lease_ms, bool) or not isinstance(lease_ms, int) or lease_ms < 1:
            raise ValueError("lease_ms must be a positive integer")

    @staticmethod
    def _text(value) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)
