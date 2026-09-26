"""Kafka adapter — aiokafka-based MessageBus implementation."""

from __future__ import annotations

import json as _json
from typing import Any

from velox.hexagonal import MessageBus

__all__ = ["KafkaMessageBus"]


class KafkaMessageBus(MessageBus):
    """Async Kafka message bus via aiokafka.

    Usage:
        bus = KafkaMessageBus(bootstrap_servers="localhost:9092")
        await bus.start()
        await bus.publish("orders", {"order_id": "1"})
    """

    def __init__(self, bootstrap_servers: str = "localhost:9092", client_id: str = "velox"):
        self._servers = bootstrap_servers
        self._client_id = client_id
        self._producer = None
        self._consumer = None

    async def start(self) -> None:
        from aiokafka import AIOKafkaProducer

        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._servers,
            client_id=self._client_id,
            value_serializer=lambda v: _json.dumps(v).encode(),
        )
        await self._producer.start()

    async def stop(self) -> None:
        if self._producer:
            await self._producer.stop()
        if self._consumer:
            await self._consumer.stop()

    async def publish(self, topic: str, message: dict, key: str | None = None) -> None:
        if self._producer is None:
            raise RuntimeError("Kafka not started. Call await bus.start()")
        await self._producer.send_and_wait(
            topic,
            message,
            key=key.encode() if key else None,
        )

    async def subscribe(self, topic: str, handler) -> None:
        from aiokafka import AIOKafkaConsumer

        self._consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=self._servers,
            group_id=f"{self._client_id}-{topic}",
            value_deserializer=lambda v: _json.loads(v.decode()),
        )
        await self._consumer.start()

        async def _poll():
            async for msg in self._consumer:
                result = handler(msg.value)
                if hasattr(result, "__await__"):
                    await result

        import asyncio
        asyncio.create_task(_poll())
