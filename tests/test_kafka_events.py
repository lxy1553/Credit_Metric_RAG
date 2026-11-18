from types import SimpleNamespace

from credit.kafka_events import consume, decode, encode, publish
from credit.realtime import LoanEvent


def event():
    return LoanEvent(event_id="K-1", loan_id="L001", biz_date="2026-03-02", version=2,
                     product="consumer", domain="credit", balance_cents=800_000, overdue_cents=0)


def test_kafka_event_encoding_round_trip():
    assert decode(encode(event())) == event()


def test_publish_waits_for_broker_confirmation():
    sent = []

    class Producer:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def send(self, topic, **kwargs):
            sent.append((topic, kwargs))
            return SimpleNamespace(get=lambda timeout: SimpleNamespace(partition=0, offset=7))

        def flush(self, timeout):
            assert timeout == 10

        def close(self):
            pass

    result = publish(event(), bootstrap_servers="kafka:9092", producer_factory=Producer)
    assert result == {"topic": "credit.loan-events.v1", "partition": 0, "offset": 7, "event_id": "K-1"}
    assert sent[0][1]["key"] == "L001"
    assert sent[0][1]["value"] == encode(event())


def test_consumer_commits_only_after_safe_replay(db):
    commits = []

    class Consumer:
        def __init__(self, *args, **kwargs):
            self.items = [SimpleNamespace(value=encode(event()))]

        def poll(self, **kwargs):
            items, self.items = self.items, []
            return {"partition-0": items}

        def commit(self):
            commits.append(True)

        def close(self):
            pass

    result = consume(db, bootstrap_servers="kafka:9092", consumer_factory=Consumer)
    assert result["statuses"] == {"applied": 1}
    assert commits == [True]
