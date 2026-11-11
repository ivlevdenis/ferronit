"""Kafka adapter: contract and lifecycle without a real broker.

aiokafka is an optional dependency, so the module is replaced by a fake:
the "not started", "started" and consumer background task branches are checked.
"""

import sys
import types

import pytest

from ferronit.contrib.kafka import KafkaMessageBus


class FakeProducer:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.sent: list[tuple] = []

    async def start(self):
        self.started = True

    async def stop(self):
        self.started = False

    async def send_and_wait(self, topic, message, key=None):
        self.sent.append((topic, message, key))


class FakeConsumer:
    def __init__(self, *topics, **kwargs):
        self.topics = topics
        self.kwargs = kwargs

    async def start(self):
        pass

    async def stop(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


@pytest.fixture
def fake_aiokafka(monkeypatch):
    module = types.ModuleType("aiokafka")
    module.AIOKafkaProducer = FakeProducer  # type: ignore[attr-defined]
    module.AIOKafkaConsumer = FakeConsumer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aiokafka", module)
    return module


@pytest.mark.asyncio
async def test_publish_before_start_raises_runtime_error():
    bus = KafkaMessageBus("localhost:9092")

    with pytest.raises(RuntimeError, match="Kafka not started"):
        await bus.publish("orders", {"id": "1"})


@pytest.mark.asyncio
async def test_start_then_publish_sends_serialised_message(fake_aiokafka):
    bus = KafkaMessageBus("kafka:9092", client_id="test")

    await bus.start()
    await bus.publish("orders", {"id": "1"}, key="k1")

    assert bus._producer.kwargs["client_id"] == "test"
    assert bus._producer.sent == [("orders", {"id": "1"}, b"k1")]

    await bus.stop()
    assert bus._producer.started is False


@pytest.mark.asyncio
async def test_subscribe_keeps_task_reference_and_stop_cancels_it(fake_aiokafka):
    bus = KafkaMessageBus("kafka:9092")

    async def handler(value):
        raise AssertionError("the consumer must not receive messages in this test")

    await bus.subscribe("orders", handler)

    assert bus._consumer.topics == ("orders",)
    assert bus._poll_task is not None  # the reference is kept: otherwise GC collects the task

    await bus.stop()
    assert bus._poll_task is None


@pytest.mark.asyncio
async def test_stop_without_start_is_safe():
    await KafkaMessageBus().stop()  # no producer, no consumer, no task
