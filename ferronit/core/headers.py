"""Ordered HTTP field lines — implemented in the Rust core.

A response field section is a sequence, not a mapping: order is significant and
repeated field names are distinct lines (RFC 9110 §5.3). ``Set-Cookie`` must
never be comma-joined (RFC 6265 §4.1.1), while other repeated fields may be
combined by a recipient (§5.2) — so a dict cannot represent headers faithfully.

The container itself lives in :mod:`ferronit._core` (``#[pyclass] Headers``):
iteration yields ``(name, value)`` pairs in insertion order, names are
lowercased RFC 9110 tokens, values are latin-1 with CR/LF/NUL replaced by SP,
other CTLs rejected, and leading/trailing OWS stripped. Name lookup is
case-insensitive and combines repeats with ``", "`` except ``set-cookie``,
whose lookup returns the first line; use :meth:`Headers.get_all` for every
value. This module only exposes the type and its input alias.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ferronit._core import Headers

__all__ = ["HeaderInput", "Headers"]

type HeaderInput = Mapping[str, str] | Iterable[tuple[str, str]] | Headers | None
