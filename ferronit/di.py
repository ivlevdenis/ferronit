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

    __slots__ = ("_factories", "_instances")

    def __init__(self) -> None:
        self._instances: dict[Any, Any] = {}
        self._factories: dict[Any, type] = {}

    def singleton(self, key: Any, instance: Any) -> None:
        """Register an already-constructed instance under a type or string key.

        Args:
            key: Lookup key (usually a type or a string name).
            instance: Shared instance returned by :meth:`get`.
        """
        self._instances[key] = instance

    def factory(self, key: Any, concrete: type | None = None) -> None:
        """Register a type to be constructed lazily on the first ``get``.

        Args:
            key: Lookup key (usually a type or a string name).
            concrete: Concrete class to instantiate; defaults to ``key`` itself
                when the key already is the class to build.
        """
        self._factories[key] = concrete or key

    def get(self, target: Any) -> Any:
        """Resolve a dependency by key, instantiating and caching it if needed.

        Args:
            target: Registered type or string key.

        Returns:
            The registered instance, or a freshly built one for factory keys.

        Raises:
            KeyError: If the key was never registered as a singleton or factory.
        """
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
            # сигнатура класса — это __init__ без self; eval_str разворачивает
            # строковые аннотации (PEP 563 / `from __future__ import annotations`)
            sig = inspect.signature(concrete, eval_str=True)
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
                else:
                    raise TypeError(
                        f"Cannot resolve dependency '{name}: {ann}' for "
                        f"{concrete.__name__} — зарегистрируй её в контейнере"
                    ) from None

        return concrete(**kwargs)
