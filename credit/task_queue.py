"""Redis Streams transport for encrypted question task payloads."""

from dataclasses import dataclass, field

from redis.exceptions import ResponseError

from credit.task_payloads import InvalidTaskPayload, TaskPayloadCipher


@dataclass(frozen=True)
class ClaimedTask:
    message_id: str
    request_id: str
    payload: str = field(repr=False)


class RedisTaskQueue:
    """Small Redis Streams adapter; task metadata/state remain a separate concern."""

    def __init__(self, client, cipher: TaskPayloadCipher, *, stream="credit:question-tasks",
                 group="credit-question-workers"):
        if not stream or not group:
            raise ValueError("stream and consumer group must be non-empty")
        self.client = client
        self.cipher = cipher
        self.stream = stream
        self.group = group

    def prepare(self) -> None:
        try:
            self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def enqueue(self, request_id: str, payload: str) -> str:
        encrypted = self.cipher.encrypt(payload, request_id=request_id)
        message_id = self.client.xadd(self.stream, {
            "request_id": request_id,
            "payload": encrypted,
        })
        return self._text(message_id)

    def claim(self, consumer: str, *, count=1, block_ms=1000) -> list[ClaimedTask]:
        if not consumer or count < 1 or block_ms < 0:
            raise ValueError("consumer must be set, count positive and block_ms non-negative")
        response = self.client.xreadgroup(
            self.group, consumer, {self.stream: ">"}, count=count, block=block_ms,
        )
        return [self._claimed(message_id, fields)
                for _, messages in response for message_id, fields in messages]

    def reclaim(self, consumer: str, *, min_idle_ms: int, count=10) -> list[ClaimedTask]:
        if not consumer or min_idle_ms < 0 or count < 1:
            raise ValueError("consumer must be set, min_idle_ms non-negative and count positive")
        response = self.client.xautoclaim(
            self.stream, self.group, consumer, min_idle_ms, start_id="0-0", count=count,
        )
        messages = response[1]
        return [self._claimed(message_id, fields) for message_id, fields in messages]

    def ack(self, message_id: str) -> bool:
        return bool(self.client.xack(self.stream, self.group, message_id))

    def ack_and_delete(self, message_id: str) -> bool:
        """Atomically acknowledge and remove the encrypted payload from the stream."""
        script = """
        local acknowledged = redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
        if acknowledged == 1 then return redis.call('XDEL', KEYS[1], ARGV[2]) end
        return 0
        """
        return bool(self.client.eval(script, 1, self.stream, self.group, message_id))

    def _claimed(self, message_id, fields) -> ClaimedTask:
        normalized = {self._text(key): self._text(value) for key, value in fields.items()}
        request_id = normalized.get("request_id", "")
        encrypted = normalized.get("payload", "")
        if not request_id or not encrypted:
            raise InvalidTaskPayload("task payload authentication failed")
        payload = self.cipher.decrypt(encrypted, request_id=request_id)
        return ClaimedTask(self._text(message_id), request_id, payload)

    @staticmethod
    def _text(value) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)
