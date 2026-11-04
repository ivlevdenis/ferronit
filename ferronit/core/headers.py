"""Ordered HTTP field lines — the RFC 9110 representation of a field section.
A response field section is a sequence, not a mapping: field order is
significant, and repeated field names are distinct lines (RFC 9110 §5.3).
``Set-Cookie`` explicitly must never be comma-joined (RFC 6265 §4.1.1), while
other repeated fields may be combined by a recipient (§5.2) — so a plain
``dict`` cannot represent headers faithfully.

Iteration over :class:`Headers` yields ``(name, value)`` pairs in insertion
order. Name lookup stays case-insensitive and combines repeated fields with
``", "`` except ``set-cookie``, whose lookup returns the first line; use
:meth:`Headers.get_all` when every value matters.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping

__all__ = ["HeaderInput", "Headers"]

type HeaderInput = Mapping[str, str] | Iterable[tuple[str, str]] | Headers | None

# RFC 9110 §5.6.2 token characters.
_TOKEN_CHARS = frozenset(
    "!#$%&'*+-.^_`|~"
    "0123456789"
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
)

# Repeated ``Set-Cookie`` lines are never equivalent to a comma-joined value.
_NO_COMBINE = "set-cookie"

# RFC 9110 §5.5: CR, LF and NUL are replaced with SP (the sanctioned rewrite);
# every other CTL is invalid with no rewrite and is rejected instead.
_SP_FOR_DANGEROUS = re.compile(r"[\x00\n\r]")
_OTHER_CTL = re.compile(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]")

# OWS around a field value is not part of the value (RFC 9110 §5.5, §5.6.3).
_OWS = " \t"

def _field_name(raw: str) -> str:
    """Lowercase and validate a field name as an RFC 9110 token."""
    if not isinstance(raw, str):
        raise ValueError(f"Header name must be str, got {type(raw).__name__}")
    name = raw.lower()
    if not name or any(ch not in _TOKEN_CHARS for ch in name):
        raise ValueError(f"Invalid header name: {raw!r}")
    return name


def _field_value(raw: str) -> str:
    """Normalise a field value to RFC 9110 §5.5 semantics.

    The grammar allows HTAB, SP, VCHAR and obs-text (0x80-0xFF) in a value.
    CR, LF and NUL are invalid and dangerous: the spec permits a recipient to
    reject the message or replace each of those characters with SP, and SP
    replacement is what a sender can apply without dropping data. Every other
    CTL is invalid with no sanctioned rewrite, so it is rejected. Leading and
    trailing OWS is excluded because it is not part of the value. Values that
    are not latin-1 encodable cannot go on the wire unchanged and are rejected
    (encode with RFC 8187 first).
    """
    if not isinstance(raw, str):
        raise ValueError(f"Header value must be str, got {type(raw).__name__}")
    try:
        raw.encode("latin-1")
    except UnicodeEncodeError:
        raise ValueError(
            f"Header value is not latin-1 encodable: {raw!r}; "
            "encode it (RFC 8187) before setting it"
        ) from None
    invalid = _OTHER_CTL.search(raw)
    if invalid is not None:
        raise ValueError(
            f"Header value contains a control character {invalid.group()!r}"
        )
    return _SP_FOR_DANGEROUS.sub(" ", raw).strip(_OWS)


class Headers:
    """Mutable sequence of HTTP field lines with duplicates preserved.

    Args:
        items: Initial field lines — another :class:`Headers`, a mapping, or an
            iterable of ``(name, value)`` pairs. A mapping cannot express
            duplicates; pairs can.
    """

    __slots__ = ("_lines",)

    def __init__(self, items: HeaderInput = None) -> None:
        self._lines: list[tuple[str, str]] = []
        if items is None:
            return
        if isinstance(items, Headers):
            self._lines.extend(items._lines)
        elif isinstance(items, Mapping):
            for name, value in items.items():
                self.add(name, value)
        else:
            for name, value in items:
                self.add(name, value)

    # ── mutation ───────────────────────────────────────────────────────

    def add(self, name: str, value: str) -> None:
        """Append one field line, keeping any existing line with the same name."""
        self._lines.append((_field_name(name), _field_value(value)))

    def _prepend(self, name: str, value: str) -> None:
        """Insert a field line before every existing one."""
        self._lines.insert(0, (_field_name(name), _field_value(value)))

    def __setitem__(self, name: str, value: str) -> None:
        """Replace every line of ``name`` with one line at the first position."""
        key = _field_name(name)
        item = (key, _field_value(value))
        lines: list[tuple[str, str]] = []
        replaced = False
        for existing, current in self._lines:
            if existing != key:
                lines.append((existing, current))
            elif not replaced:
                lines.append(item)
                replaced = True
        if not replaced:
            lines.append(item)
        self._lines = lines

    def __delitem__(self, name: str) -> None:
        key = _field_name(name)
        remaining = [line for line in self._lines if line[0] != key]
        if len(remaining) == len(self._lines):
            raise KeyError(name)
        self._lines = remaining

    def setdefault(self, name: str, value: str) -> str:
        """Return the current value, or append ``value`` and return it."""
        if name in self:
            return self[name]
        self.add(name, value)
        return value

    def pop(self, name: str, default: str | None = None) -> str | None:
        """Remove every line of ``name`` and return the first value."""
        try:
            value = self[name]
        except KeyError:
            return default
        del self[name]
        return value

    # ── lookup ─────────────────────────────────────────────────────────

    def __getitem__(self, name: str) -> str:
        key = _field_name(name)
        values = [value for existing, value in self._lines if existing == key]
        if not values:
            raise KeyError(name)
        if key == _NO_COMBINE:
            return values[0]
        return ", ".join(values)

    def get(self, name: str, default: str | None = None) -> str | None:
        """Combined value of ``name``, or ``default`` when absent."""
        try:
            return self[name]
        except KeyError:
            return default

    def get_all(self, name: str) -> list[str]:
        """Every value of ``name`` in wire order; empty when absent."""
        key = _field_name(name)
        return [value for existing, value in self._lines if existing == key]

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        try:
            key = _field_name(name)
        except ValueError:
            return False
        return any(existing == key for existing, _ in self._lines)

    # ── sequence protocol ──────────────────────────────────────────────

    def __iter__(self) -> Iterator[tuple[str, str]]:
        """Yield ``(name, value)`` field lines in insertion order."""
        return iter(self._lines)

    def __len__(self) -> int:
        """Number of field lines, counting duplicates separately."""
        return len(self._lines)

    def keys(self) -> list[str]:
        """Field names in wire order, duplicates included."""
        return [name for name, _ in self._lines]

    def values(self) -> list[str]:
        """Field values in wire order."""
        return [value for _, value in self._lines]

    def items(self) -> list[tuple[str, str]]:
        """Same ``(name, value)`` pairs as iteration."""
        return list(self._lines)

    # ── wire encodings ─────────────────────────────────────────────────

    def raw(self) -> list[tuple[bytes, bytes]]:
        """ASGI ``http.response.start`` list: lowercase names, latin-1 bytes."""
        return [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in self._lines
        ]

    def str_pairs(self) -> list[tuple[str, str]]:
        """RSGI ``response_*`` list: string pairs, order and duplicates kept."""
        return list(self._lines)

    def __repr__(self) -> str:
        return f"Headers({self._lines!r})"
