"""Radix tree router — O(path_length) lookup, no regex overhead."""

from __future__ import annotations

__all__ = ["Router"]


class _Node:
    """Radix tree node."""

    __slots__ = ("prefix", "methods", "children", "param_name")

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        self.methods: dict[str, object] = {}  # METHOD → handler
        self.children: list[_Node] = []
        self.param_name: str | None = None


class Router:
    """Compiled route table — radix tree per method type."""

    __slots__ = ("_root",)

    def __init__(self) -> None:
        self._root = _Node()

    def add(self, method: str, path: str, handler: object) -> None:
        """Register a route handler for a method + path pattern."""
        method = method.upper()
        if path == "/":
            self._root.methods[method] = handler
            return

        node = self._root
        segments = path.strip("/").split("/")

        for seg in segments:
            if seg.startswith("{") and seg.endswith("}"):
                # Parameterised segment
                param_name = seg[1:-1]
                child = _find_child_by_param(node)
                if child is None:
                    child = _Node(f"{{{param_name}}}")
                    child.param_name = param_name
                    node.children.append(child)
                node = child
            else:
                # Static segment
                child = _find_child(node, seg)
                if child is None:
                    child = _Node(seg)
                    node.children.append(child)
                node = child

        node.methods[method] = handler

    def lookup(self, method: str, path: str) -> tuple[object | None, dict[str, str]]:
        """Find handler and path parameters. Returns (handler, params)."""
        method = method.upper()
        params: dict[str, str] = {}

        if path == "/":
            return self._root.methods.get(method), params

        node = self._root
        segments = path.strip("/").split("/")

        for seg in segments:
            # Try static match first
            child = _find_child(node, seg)
            if child is not None:
                node = child
                continue

            # Try parameterised match
            child = _find_child_by_param(node)
            if child is not None:
                assert child.param_name is not None
                params[child.param_name] = seg
                node = child
                continue

            return None, {}

        return node.methods.get(method), params


def _find_child(node: _Node, prefix: str) -> _Node | None:
    for child in node.children:
        if child.prefix == prefix and child.param_name is None:
            return child
    return None


def _find_child_by_param(node: _Node) -> _Node | None:
    for child in node.children:
        if child.param_name is not None:
            return child
    return None
