"""Kafka 单 Broker 演示：受校验事件发布、手动提交消费与 REALTIME ADS 更新。"""

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path

from credit.realtime import LoanEvent, replay

DEFAULT_TOPIC = "credit.loan-events.v1"


def _require_kafka():
    try:
        from kafka import KafkaConsumer, KafkaProducer
        from kafka.admin import KafkaAdminClient, NewTopic
    except ImportError as exc:  # pragma: no cover - depends on optional local runtime
        raise RuntimeError("未安装 Kafka 依赖；请执行 .venv/bin/python -m pip install -e '.[kafka]'") from exc
    return KafkaProducer, KafkaConsumer, KafkaAdminClient, NewTopic


def encode(event: LoanEvent) -> bytes:
    """唯一的线上消息格式；先校验模型，再序列化。"""
    return event.model_dump_json().encode("utf-8")


def decode(payload: bytes) -> LoanEvent:
    return LoanEvent.model_validate_json(payload)


def ensure_topic(bootstrap_servers: str, topic: str = DEFAULT_TOPIC):
    """本地单 Broker 建 Topic；已存在时不修改其分区或副本配置。"""
    _, _, KafkaAdminClient, NewTopic = _require_kafka()
    admin = KafkaAdminClient(bootstrap_servers=bootstrap_servers, client_id="credit-kafka-admin")
    try:
        existing = set(admin.list_topics())
        if topic not in existing:
            admin.create_topics([NewTopic(name=topic, num_partitions=1, replication_factor=1)])
    finally:
        admin.close()


def publish(event: LoanEvent, *, bootstrap_servers: str, topic: str = DEFAULT_TOPIC,
            producer_factory: Callable | None = None) -> dict:
    """同步等待 broker 确认，避免页面或 CLI 把未确认事件显示为已发布。"""
    if producer_factory is None:
        KafkaProducer, _, _, _ = _require_kafka()
        producer_factory = KafkaProducer
    producer = producer_factory(
        bootstrap_servers=bootstrap_servers, acks="all", retries=3,
        value_serializer=lambda value: value, key_serializer=lambda value: value.encode("utf-8"),
    )
    try:
        metadata = producer.send(topic, key=event.loan_id, value=encode(event), headers=[("schema", b"loan-event-v1")]).get(
            timeout=10
        )
        producer.flush(timeout=10)
        return {"topic": topic, "partition": metadata.partition, "offset": metadata.offset, "event_id": event.event_id}
    finally:
        producer.close()


def consume(db: Path, *, bootstrap_servers: str, topic: str = DEFAULT_TOPIC, group_id: str = "credit-realtime-v1",
            max_messages: int = 100, timeout_ms: int = 1_000, consumer_factory: Callable | None = None) -> dict:
    """处理一批消息；只有可安全重放的事件才提交 offset。"""
    if max_messages < 1:
        raise ValueError("max_messages 必须大于 0")
    if consumer_factory is None:
        _, KafkaConsumer, _, _ = _require_kafka()
        consumer_factory = KafkaConsumer
    consumer = consumer_factory(
        topic, bootstrap_servers=bootstrap_servers, group_id=group_id, enable_auto_commit=False,
        auto_offset_reset="earliest", consumer_timeout_ms=timeout_ms,
    )
    statuses: dict[str, int] = {}
    processed = 0
    try:
        # 消费者首次加入 group 时，首轮 poll 可能仅完成分区分配而没有返回记录。
        # 连续五次空 poll 才认为本批次没有待处理消息，避免漏读刚发布的本地演示事件。
        empty_polls = 0
        while processed < max_messages and empty_polls < 5:
            records = consumer.poll(timeout_ms=timeout_ms, max_records=max_messages - processed)
            if not records:
                empty_polls += 1
                continue
            batch = [message for messages in records.values() for message in messages]
            if not batch:
                empty_polls += 1
                continue
            empty_polls = 0
            for message in batch:
                event = decode(message.value)
                status = replay(db, event)
                statuses[status] = statuses.get(status, 0) + 1
                consumer.commit()
                processed += 1
                if processed >= max_messages:
                    break
    finally:
        consumer.close()
    return {"topic": topic, "group_id": group_id, "processed": processed, "statuses": statuses}


def produce_main():
    parser = argparse.ArgumentParser(description="向 Kafka 发布经 LoanEvent 校验的信贷实时事件")
    parser.add_argument("event", type=Path, help="LoanEvent JSON 文件")
    parser.add_argument("--bootstrap", default=os.getenv("CREDIT_KAFKA_BOOTSTRAP", "127.0.0.1:9092"))
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    args = parser.parse_args()
    ensure_topic(args.bootstrap, args.topic)
    print(json.dumps(publish(LoanEvent.model_validate_json(args.event.read_bytes()), bootstrap_servers=args.bootstrap,
                             topic=args.topic), ensure_ascii=False))


def consume_main():
    parser = argparse.ArgumentParser(description="消费 Kafka 信贷事件并更新本地 REALTIME ADS")
    parser.add_argument("--db", type=Path, default=Path("var/credit.db"))
    parser.add_argument("--bootstrap", default=os.getenv("CREDIT_KAFKA_BOOTSTRAP", "127.0.0.1:9092"))
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--group", default="credit-realtime-v1")
    parser.add_argument("--max-messages", type=int, default=100)
    args = parser.parse_args()
    ensure_topic(args.bootstrap, args.topic)
    print(json.dumps(consume(args.db, bootstrap_servers=args.bootstrap, topic=args.topic, group_id=args.group,
                             max_messages=args.max_messages), ensure_ascii=False))


if __name__ == "__main__":
    consume_main()
