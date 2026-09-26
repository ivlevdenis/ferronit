"""Dependency injection container — type/string-based, zero-reflect, lazy."""

from __future__ import annotations

from typing import Any

__all__ = ["Container"]


class Container:
    """Simple DI container.

    Usage:
        c = Container()
        c.singleton(Config, EnvConfig())
        c.singleton("uow", PostgresUoW())
        c.factory("CartService", CartService)

        svc = c.get("CartService")
    """

    __slots__ = ("_instances", "_factories")

    def __init__(self) -> None:
        self._instances: dict[Any, Any] = {}
        self._factories: dict[Any, type] = {}

    def singleton(self, key: Any, instance: Any) -> None:
        self._instances[key] = instance

    def factory(self, key: Any, concrete: type | None = None) -> None:
        self._factories[key] = concrete or key

    def get(self, target: Any) -> Any:
        if target in self._instances:
            return self._instances[target]
        if target in self._factories:
            return self._resolve(self._factories[target])
        raise KeyError(f"Not registered: {target!r}")

    def _resolve(self, concrete: type) -> Any:
        if concrete in self._instances:
            return self._instances[concrete]

        import inspect
        try:
            sig = inspect.signature(concrete.__init__)
        except (ValueError, TypeError):
            return concrete()

        kwargs = {}
        for name, param in sig.parameters.items():
            if name == "self":
                continue
            ann = param.annotation
            if ann is inspect.Parameter.empty:
                if param.default is not inspect.Parameter.empty:
                    continue
                raise TypeError(f"Cannot resolve {name} for {concrete.__name__}")
            try:
                kwargs[name] = self.get(ann)
            except KeyError:
                if param.default is not inspect.Parameter.empty:
                    kwargs[name] = param.default

        return concrete(**kwargs)
