"""Минимальный клиент PostgreSQL по проводу — без драйвера.

Зачем: asyncpg отдаёт ~33 тыс. запросов/с на выборке ста строк, но сколько из этого
принадлежит самому драйверу, а сколько — сокету и протоколу, не видно. Здесь протокол
реализован вручную (startup, SASL/SCRAM-SHA-256, простой запрос), чтобы было с чем сравнить.

Что умеет: подключение с аутентификацией SCRAM, простой запрос, пакет запросов одним
flush (пайплайн), разбор RowDescription/DataRow и минимальное приведение типов
(int4/int8 → int, текстовые → str).

Чего не умеет: prepared statements, бинарный формат, COPY, уведомления, пул соединений.
Это стенд, а не замена драйверу.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import socket
import struct
from typing import Any

__all__ = ["PreparedStatement", "WireConnection", "WireError"]

PROTOCOL_VERSION = 196608  # 3.0

AUTH_OK = 0
AUTH_SASL = 10
AUTH_SASL_CONTINUE = 11
AUTH_SASL_FINAL = 12

_MSG_READY = b"Z"
_MSG_ROW_DESCRIPTION = b"T"
_MSG_DATA_ROW = b"D"
_MSG_COMMAND_COMPLETE = b"C"
_MSG_ERROR = b"E"

# OID типов, которые нам нужны для приведения значений
_INT_OIDS = {20, 21, 23, 26}  # int8, int2, int4, oid


class WireError(RuntimeError):
    """Server-side error, raised with the PostgreSQL message text."""


def _scram_client_first() -> tuple[str, str]:
    """Build the SCRAM client-first message.

    Returns:
        A tuple of the full message (``n,,...``) and its bare form used later
        inside ``AuthMessage``.
    """
    nonce = base64.b64encode(os.urandom(18)).decode()
    bare = f"n=,r={nonce}"
    return f"n,,{bare}", bare


def _scram_client_final(
    server_first: str, password: str, client_first_bare: str
) -> tuple[str, bytes]:
    """Build the SCRAM client-final message and the expected server signature.

    Args:
        server_first: The server-first message (``r=...,s=...,i=...``).
        password: Plain-text password.
        client_first_bare: Bare client-first message from earlier.

    Returns:
        A tuple of the client-final message and the server signature we expect
        back, so the handshake can be verified rather than trusted.
    """
    fields = dict(part.split("=", 1) for part in server_first.split(","))
    salt = base64.b64decode(fields["s"])
    iterations = int(fields["i"])
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    final_without_proof = f"c=biws,r={fields['r']}"
    auth_message = f"{client_first_bare},{server_first},{final_without_proof}"
    signature = hmac.new(stored_key, auth_message.encode(), hashlib.sha256).digest()
    proof = bytes(a ^ b for a, b in zip(client_key, signature))
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    expected = hmac.new(server_key, auth_message.encode(), hashlib.sha256).digest()
    return f"{final_without_proof},p={base64.b64encode(proof).decode()}", expected


class PreparedStatement:
    """A server-side prepared statement, produced by ``WireConnection.prepare``.

    Attributes:
        names: Result column names.
        oids: Result column type OIDs.
    """

    __slots__ = ("conn", "name", "names", "oids")

    def __init__(self, conn: "WireConnection", name: str, names: list[str], oids: list[int]) -> None:
        self.conn = conn
        self.name = name
        self.names = names
        self.oids = oids


class WireConnection:
    """A minimal PostgreSQL connection speaking the v3 protocol over asyncio streams."""

    __slots__ = ("_counter", "_reader", "_writer")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._counter = 0

    @classmethod
    async def connect(
        cls,
        host: str = "127.0.0.1",
        port: int = 5432,
        user: str = "postgres",
        password: str = "postgres",
        database: str = "postgres",
    ) -> WireConnection:
        """Open a connection and complete the startup handshake.

        Args:
            host: Server host.
            port: Server port.
            user: Role name.
            password: Password (SCRAM-SHA-256).
            database: Database name.

        Returns:
            A connected ``WireConnection``.

        Raises:
            WireError: If the server rejects startup or authentication.
        """
        reader, writer = await asyncio.open_connection(host, port)
        # asyncio НЕ ставит TCP_NODELAY, а без него мелкие записи ждут
        # подтверждения (Nagle + delayed ACK) — на каждом круге это десятки микросекунд.
        # asyncpg, к слову, этот флаг выставляет.
        raw_socket = writer.get_extra_info("socket")
        if raw_socket is not None:
            raw_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn = cls(reader, writer)
        payload = struct.pack("!I", PROTOCOL_VERSION)
        for key, value in (("user", user), ("database", database), ("application_name", "wire")):
            payload += key.encode() + b"\0" + value.encode() + b"\0"
        payload += b"\0"
        writer.write(struct.pack("!I", len(payload) + 4) + payload)
        await writer.drain()
        await conn._authenticate(password)
        return conn

    async def _authenticate(self, password: str) -> None:
        """Run the authentication exchange, if the server asks for one."""
        client_first_bare = ""
        expected_signature: bytes | None = None
        while True:
            kind, body = await self._read_message()
            if kind == _MSG_ERROR:
                raise WireError(_error_text(body))
            if kind != b"R":
                if kind == _MSG_READY:
                    return
                continue
            code = struct.unpack("!I", body[:4])[0]
            if code == AUTH_OK:
                continue
            if code != AUTH_SASL:
                raise WireError(f"неподдерживаемый метод аутентификации: {code}")
            client_first, client_first_bare = _scram_client_first()
            data = client_first.encode()
            # SASLInitialResponse: имя механизма (cstring) + длина ответа + ответ
            payload = b"SCRAM-SHA-256\0" + struct.pack("!I", len(data)) + data
            self._writer.write(b"p" + struct.pack("!I", len(payload) + 4) + payload)
            await self._writer.drain()

            kind, body = await self._read_message()
            if kind == _MSG_ERROR:
                raise WireError(_error_text(body))
            code = struct.unpack("!I", body[:4])[0]
            if code != AUTH_SASL_CONTINUE:
                raise WireError(f"ожидался SASLContinue, получен код {code}")
            server_first = body[4:].decode()
            client_final, expected_signature = _scram_client_final(
                server_first, password, client_first_bare
            )
            data = client_final.encode()
            self._writer.write(b"p" + struct.pack("!I", len(data) + 4) + data)
            await self._writer.drain()

            kind, body = await self._read_message()
            if kind == _MSG_ERROR:
                raise WireError(_error_text(body))
            code = struct.unpack("!I", body[:4])[0]
            if code != AUTH_SASL_FINAL:
                raise WireError(f"ожидался SASLFinal, получен код {code}")
            signature = base64.b64decode(dict(
                part.split("=", 1) for part in body[4:].decode().split(",")
            )["v"])
            if expected_signature is not None and signature != expected_signature:
                raise WireError("подпись сервера не совпала — обмен повреждён")

    async def query(self, sql: str) -> list[dict[str, Any]]:
        """Run one query and return its rows as dicts.

        Args:
            sql: SQL text.

        Returns:
            Rows as dicts with minimal type conversion (int columns → ``int``).
        """
        self._send_query(sql)
        await self._writer.drain()
        return await self._read_result()

    async def prepare(self, sql: str) -> "PreparedStatement":
        """Parse and describe a statement once, server-side.

        This is what a driver does under the hood: the server parses and plans
        the query a single time, and later executions skip that work.

        Args:
            sql: SQL text to prepare.

        Returns:
            A ``PreparedStatement`` handle usable with ``fetch_prepared``.
        """
        name = f"wire_{id(sql):x}_{self._counter}"
        self._counter += 1
        data = name.encode() + b"\0" + sql.encode() + b"\0" + struct.pack("!H", 0)
        self._writer.write(b"P" + struct.pack("!I", len(data) + 4) + data)
        data = b"S" + name.encode() + b"\0"
        self._writer.write(b"D" + struct.pack("!I", len(data) + 4) + data)
        self._writer.write(b"S" + struct.pack("!I", 4))
        await self._writer.drain()
        names: list[str] = []
        oids: list[int] = []
        while True:
            kind, body = await self._read_message()
            if kind == _MSG_ERROR:
                raise WireError(_error_text(body))
            if kind == _MSG_ROW_DESCRIPTION:
                names, oids = _parse_row_description(body)
            elif kind == _MSG_READY:
                return PreparedStatement(self, name, names, oids)

    async def fetch_prepared(self, statement: "PreparedStatement") -> list[dict[str, Any]]:
        """Execute a prepared statement in binary format and return its rows.

        Args:
            statement: Handle from ``prepare``.

        Returns:
            Rows as dicts, decoded from the binary wire format.
        """
        bind = (
            b"\0"  # unnamed portal
            + statement.name.encode()
            + b"\0"
            + struct.pack("!H", 0)  # нет форматов параметров
            + struct.pack("!H", 0)  # нет параметров
            + struct.pack("!H", 1)  # один формат для всех колонок результата
            + struct.pack("!H", 1)  # бинарный
        )
        self._writer.write(b"B" + struct.pack("!I", len(bind) + 4) + bind)
        data = b"\0" + struct.pack("!I", 0)
        self._writer.write(b"E" + struct.pack("!I", len(data) + 4) + data)
        self._writer.write(b"S" + struct.pack("!I", 4))
        await self._writer.drain()

        rows: list[dict[str, Any]] = []
        while True:
            kind, body = await self._read_message()
            if kind == _MSG_ERROR:
                raise WireError(_error_text(body))
            if kind == _MSG_DATA_ROW:
                rows.append(_parse_binary_row(body, statement.names, statement.oids))
            elif kind == _MSG_READY:
                return rows

    async def query_pipelined(self, sql: str, count: int) -> int:
        """Send the same query ``count`` times in one flush and read all results.

        This is what a driver with batching does: one syscall round instead of
        ``count`` of them. No statement is awaited before the next is written.

        Args:
            sql: SQL text to repeat.
            count: How many statements to pipeline.

        Returns:
            The number of rows read across all responses.
        """
        for _ in range(count):
            self._send_query(sql)
        await self._writer.drain()
        rows = 0
        for _ in range(count):
            rows += len(await self._read_result())
        return rows

    def _send_query(self, sql: str) -> None:
        data = sql.encode()
        self._writer.write(b"Q" + struct.pack("!I", len(data) + 5) + data + b"\0")

    async def _read_message(self) -> tuple[bytes, bytes]:
        header = await self._reader.readexactly(5)
        length = struct.unpack("!I", header[1:5])[0]
        body = await self._reader.readexactly(length - 4) if length > 4 else b""
        return header[:1], body

    async def _read_result(self) -> list[dict[str, Any]]:
        names: list[str] = []
        oids: list[int] = []
        rows: list[dict[str, Any]] = []
        while True:
            kind, body = await self._read_message()
            if kind == _MSG_ROW_DESCRIPTION:
                names, oids = _parse_row_description(body)
            elif kind == _MSG_DATA_ROW:
                rows.append(_parse_data_row(body, names, oids))
            elif kind == _MSG_ERROR:
                raise WireError(_error_text(body))
            elif kind == _MSG_READY:
                return rows

    def close(self) -> None:
        """Close the underlying socket."""
        self._writer.close()


def _parse_row_description(body: bytes) -> tuple[list[str], list[int]]:
    """Parse a RowDescription message.

    Args:
        body: Message payload.

    Returns:
        Field names and their type OIDs.
    """
    count = struct.unpack("!H", body[:2])[0]
    names: list[str] = []
    oids: list[int] = []
    offset = 2
    for _ in range(count):
        end = body.index(b"\0", offset)
        names.append(body[offset:end].decode())
        offset = end + 1
        oids.append(struct.unpack("!I", body[offset + 6 : offset + 10])[0])
        offset += 18
    return names, oids


def _parse_data_row(body: bytes, names: list[str], oids: list[int]) -> dict[str, Any]:
    """Parse a text-format DataRow message into a dict.

    Args:
        body: Message payload.
        names: Column names from RowDescription.
        oids: Column type OIDs from RowDescription.

    Returns:
        The row as a dict; ``NULL`` becomes ``None``.
    """
    count = struct.unpack("!H", body[:2])[0]
    row: dict[str, Any] = {}
    offset = 2
    for index in range(count):
        length = struct.unpack("!i", body[offset : offset + 4])[0]
        offset += 4
        if length == -1:
            row[names[index]] = None
            continue
        raw = body[offset : offset + length]
        offset += length
        text = raw.decode()
        row[names[index]] = int(text) if oids[index] in _INT_OIDS else text
    return row


def _parse_binary_row(body: bytes, names: list[str], oids: list[int]) -> dict[str, Any]:
    """Parse a binary-format DataRow message into a dict.

    Binary results skip text parsing entirely: numbers come as raw big-endian
    bytes and text as UTF-8, so no ``int()`` over a string is needed.

    Args:
        body: Message payload.
        names: Column names from RowDescription.
        oids: Column type OIDs from RowDescription.

    Returns:
        The row as a dict; ``NULL`` becomes ``None``.
    """
    count = struct.unpack("!H", body[:2])[0]
    row: dict[str, Any] = {}
    offset = 2
    for index in range(count):
        length = struct.unpack("!i", body[offset : offset + 4])[0]
        offset += 4
        if length == -1:
            row[names[index]] = None
            continue
        raw = body[offset : offset + length]
        offset += length
        oid = oids[index]
        if oid in (21, 23):
            value: Any = struct.unpack("!i" if oid == 23 else "!h", raw)[0]
        elif oid == 20:
            value = struct.unpack("!q", raw)[0]
        elif oid == 16:
            value = raw == b"\x01"
        elif oid == 700:
            value = struct.unpack("!f", raw)[0]
        elif oid == 701:
            value = struct.unpack("!d", raw)[0]
        elif oid in (19, 25, 1042, 1043):
            value = raw.decode()
        else:
            value = raw
        row[names[index]] = value
    return row


def _error_text(body: bytes) -> str:
    """Extract the human-readable part of an ErrorResponse message."""
    parts = body.split(b"\0")
    for part in parts:
        if part[:1] == b"M":
            return part[1:].decode()
    return "неизвестная ошибка сервера"
