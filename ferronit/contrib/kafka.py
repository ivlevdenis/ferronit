"""Kafka adapter — aiokafka-based MessageBus implementation."""

from __future__ import annotations

import json as _json
from typing import Any

from ferronit.hexagonal import MessageBus

__all__ = ["KafkaMessageBus"]


class KafkaMessageBus(MessageBus):
    """Async Kafka message bus via aiokafka.

    Usage:
        bus = KafkaMessageBus(bootstrap_servers="localhost:9092")
        await bus.start()
        await bus.publish("orders", {"order_id": "1"})
    """

    def __init__(self, bootstrap_servers: str = "localhost:9092", client_id: str = "ferronit"):
        self._servers = bootstrap_servers
        self._client_id = client_id
        self._producer = None
        self._consumer = None
        # держим ссылку на фоновую задачу потребителя: без неё GC может её снять
        self._poll_task: Any | None = None

    async def start(self) -> None:
        """Start the underlying Kafka producer.

        Must be awaited before :meth:`publish` is called.
        """
        from aiokafka import AIOKafkaProducer

        producer = AIOKafkaProducer(
            bootstrap_servers=self._servers,
            client_id=self._client_id,
            value_serializer=lambda v: _json.dumps(v).encode(),
        )
        await producer.start()
        self._producer = producer

    async def stop(self) -> None:
        """Cancel the consumer task and stop the producer and consumer.

        Safe to call when the bus was never started or has already stopped.
        """
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        if self._producer:
            await self._producer.stop()
        if self._consumer:
            await self._consumer.stop()

    async def publish(self, topic: str, message: dict, key: str | None = None) -> None:
        """Send a message to a topic and wait for acknowledgement.

        Args:
            topic: Target Kafka topic.
            message: JSON-serialisable payload.
            key: Optional partition key, encoded as UTF-8 bytes.

        Raises:
            RuntimeError: If :meth:`start` has not been awaited.
        """
        if self._producer is None:
            raise RuntimeError("Kafka not started. Call await bus.start()")
        await self._producer.send_and_wait(
            topic,
            message,
            key=key.encode() if key else None,
        )

    async def subscribe(self, topic: str, handler) -> None:
        """Subscribe to a topic and consume messages in a background task.

        Each decoded message value is passed to ``handler``; awaitable
        results are awaited. The background consumer task is retained on the
        instance and cancelled by :meth:`stop`.

        Args:
            topic: Kafka topic to consume.
            handler: Sync or async callable invoked with each message value.
        """
        from aiokafka import AIOKafkaConsumer

        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=self._servers,
            group_id=f"{self._client_id}-{topic}",
            value_deserializer=lambda v: _json.loads(v.decode()),
        )
        await consumer.start()
        self._consumer = consumer

        async def _poll():
            async for msg in consumer:
                result = handler(msg.value)
                if hasattr(result, "__await__"):
                    await result

        import asyncio

        # держим ссылку: без неё фоновую задачу потребителя может снять сборщик мусора
        self._poll_task = asyncio.create_task(_poll())
