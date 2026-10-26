"""Short-lived AirTouch 5 console exchanges.

The console inserts a zero after every three 0x55 bytes that follow the inner
header prefix, including the checksum. pyairtouch encodes the logical header
and checksum. This module adds that escape and reads one matching frame before
the deadline. Extended history uses message 0x1F and repeats the request.
Smart settings use that message with an FF 18 reply prefix. Preference
settings use the same framing with control message 0xC0.

While answering a status query, the console can also push an extended
message to every client. That push carries the console's own packet id, so it
is collected beside the reply rather than matched to the request. Relay
outputs read their 0x5D report and FF 04 names this way.

A program query is answered by two correlated replies, a base report and an
extension, so that exchange reads both before it closes.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from functools import partial
from typing import TYPE_CHECKING, Final, TypeVar

from pyairtouch.at5.comms.hdr import (
    ADDRESS_AIRTOUCH,
    ADDRESS_AIRTOUCH_EXTENDED,
    ADDRESS_CLIENT,
    At5Header,
    HeaderDecoder,
    HeaderEncoder,
)
from pyairtouch.at5.comms.x1F_ext import MESSAGE_ID as EXTENDED_MESSAGE_ID
from pyairtouch.at5.comms.xC0_ctrl_status import MESSAGE_ID as CONTROL_MESSAGE_ID
from pyairtouch.comms import DecodeError
from pyairtouch.comms.crc16 import Crc16Modbus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)

_OUTER_PREFIX: Final = b"\x55\x55\x55\xab"
_INNER_PREFIX: Final = b"\x55\x55\x55\xaa"
_OUTER_SIZE: Final = 10
_INNER_PREFIX_SIZE: Final = 4
_MINIMUM_INNER_SIZE: Final = 12
_MAXIMUM_FRAME_SIZE: Final = 65535
_ESCAPE_RUN: Final = 3
_ESCAPE_BYTE: Final = 0x55
_STUFF_BYTE: Final = 0x00
DEFAULT_DEADLINE: Final = 5.0
_CLOSE_TIMEOUT: Final = 1.0
_PACKET_ID_MAX: Final = 255
_ADDRESS_ALL_CLIENTS: Final = 0xFF

_ENCODER: Final = HeaderEncoder()
_DECODER: Final = HeaderDecoder()
_CRC: Final = Crc16Modbus()

_T = TypeVar("_T")


class ConsoleProtocolError(Exception):
    """A console frame was truncated, misframed, or not the requested report."""


class ConsoleChecksumError(ConsoleProtocolError):
    """A framed message failed the logical CRC."""


class ConsoleSendUncertainError(Exception):
    """The connection failed after the frame was written, so it may have arrived."""


def encode_frame(header: At5Header, payload: bytes) -> bytes:
    """Encode one console frame, including the escape bytes."""
    encoded = _ENCODER.encode(header)
    crc = _CRC.calculate(encoded.checksum_data + payload)
    logical_inner = encoded.header_bytes[_OUTER_SIZE:] + payload + crc
    if not logical_inner.startswith(_INNER_PREFIX):
        raise ConsoleProtocolError("unexpected AirTouch header layout")
    escaped = _escape(logical_inner)
    if len(escaped) > _MAXIMUM_FRAME_SIZE:
        raise ConsoleProtocolError("frame exceeds size limit")
    outer = _OUTER_PREFIX + b"\x00\x00" + struct.pack("!HH", len(escaped), len(escaped))
    return outer + escaped


class ConsoleClient:
    """Open one TCP connection per report and close it before returning."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        deadline: float = DEFAULT_DEADLINE,
    ) -> None:
        self._host = host
        self._port = port
        self._deadline = deadline
        self._lock = asyncio.Lock()
        self._packet_id = 1

    async def request(
        self,
        payload: bytes,
        *,
        response_prefix: bytes | None = None,
    ) -> bytes:
        """Send one extended request and return the bytes after the matched prefix.

        History replies repeat the request, so the default prefix is the whole
        payload. Smart settings pass an FF 18 prefix because a write is
        answered with a new body. Unrelated valid frames are ignored until
        the deadline. A bad header or checksum ends the exchange. The
        connection lock is released on cancellation.
        """
        prefix = payload if response_prefix is None else response_prefix
        reply, _ = await self._exchange(
            to_address=ADDRESS_AIRTOUCH_EXTENDED,
            message_id=EXTENDED_MESSAGE_ID,
            payload=payload,
            read=partial(_read_matching, reply_prefix=prefix, broadcast_prefix=None),
        )
        return reply[len(prefix) :]

    async def request_with_extension(
        self, payload: bytes, *, extension_prefix: bytes
    ) -> tuple[bytes, bytes]:
        """Send one extended request and return the bodies of its two replies.

        The base reply repeats the payload and the extension reply starts with
        extension_prefix. Both keep the packet id and swap the console and
        client addresses, and either can arrive first. Each body is returned
        without its prefix. Unrelated valid frames are ignored. A second reply
        with the same prefix, a bad header or checksum, or a closed connection
        ends the exchange, and both replies must arrive before the deadline.
        """
        return await self._exchange(
            to_address=ADDRESS_AIRTOUCH_EXTENDED,
            message_id=EXTENDED_MESSAGE_ID,
            payload=payload,
            read=partial(
                _read_pair, base_prefix=payload, extension_prefix=extension_prefix
            ),
        )

    async def exchange_control(self, payload: bytes) -> bytes:
        """Send one 0xC0 message and return the matching reply payload.

        History replies repeat the request. A control reply keeps the packet
        id and swaps the console and client addresses, and its subheader
        lengths replace the request, so the first matching frame is the reply.
        A setter can be answered by a report with another subcommand.
        """
        reply, _ = await self._exchange(
            to_address=ADDRESS_AIRTOUCH,
            message_id=CONTROL_MESSAGE_ID,
            payload=payload,
            read=partial(_read_matching, reply_prefix=b"", broadcast_prefix=None),
        )
        return reply

    async def query_status(
        self, payload: bytes, *, broadcast_prefix: bytes
    ) -> tuple[bytes, bytes | None]:
        """Send one 0xC0 status query and return its report and a broadcast body.

        The report also has to repeat the requested subcommand, so a
        correlated report about something else does not answer the query.
        The broadcast body is what follows broadcast_prefix in the last
        extended message to every client that arrived before the report, or
        None. The exchange does not wait for a broadcast. Any frame with a bad
        header or checksum still ends the exchange.
        """
        return await self._exchange(
            to_address=ADDRESS_AIRTOUCH,
            message_id=CONTROL_MESSAGE_ID,
            payload=payload,
            read=partial(
                _read_matching,
                reply_prefix=payload[:1],
                broadcast_prefix=broadcast_prefix,
            ),
        )

    async def send_control(self, payload: bytes) -> None:
        """Write one 0xC0 message and close without waiting for any reply.

        Returning means the frame was handed to the connection, not that the
        console accepted or acted on it. OSError or TimeoutError means nothing
        was written. ConsoleSendUncertainError means the connection failed
        after writing began. Nothing is retried.
        """
        async with self._lock:
            _, frame = self._next_frame(ADDRESS_AIRTOUCH, CONTROL_MESSAGE_ID, payload)
            writer: asyncio.StreamWriter | None = None
            try:
                async with asyncio.timeout(self._deadline):
                    _, writer = await asyncio.open_connection(self._host, self._port)
                    writer.write(frame)
                    await writer.drain()
            except (OSError, TimeoutError) as err:
                if writer is None:
                    raise
                raise ConsoleSendUncertainError("frame may have arrived") from err
            finally:
                if writer is not None:
                    await _close_writer_preserving_cancellation(writer)

    def _next_frame(
        self, to_address: int, message_id: int, payload: bytes
    ) -> tuple[At5Header, bytes]:
        packet_id = self._packet_id
        self._packet_id = 1 if packet_id == _PACKET_ID_MAX else packet_id + 1
        header = At5Header(
            to_address=to_address,
            from_address=ADDRESS_CLIENT,
            packet_id=packet_id,
            message_id=message_id,
            message_length=len(payload),
        )
        return header, encode_frame(header, payload)

    async def _exchange(
        self,
        *,
        to_address: int,
        message_id: int,
        payload: bytes,
        read: Callable[[asyncio.StreamReader, At5Header], Awaitable[_T]],
    ) -> _T:
        async with self._lock:
            request, frame = self._next_frame(to_address, message_id, payload)
            writer: asyncio.StreamWriter | None = None
            try:
                async with asyncio.timeout(self._deadline):
                    reader, writer = await asyncio.open_connection(
                        self._host, self._port
                    )
                    writer.write(frame)
                    await writer.drain()
                    try:
                        return await read(reader, request)
                    except (asyncio.IncompleteReadError, OSError) as err:
                        raise ConsoleProtocolError("connection closed") from err
            finally:
                if writer is not None:
                    await _close_writer_preserving_cancellation(writer)


def _escape(inner: bytes) -> bytes:
    """Insert a zero after every 0x55 triplet that follows the inner prefix."""
    escaped = bytearray(inner[:_INNER_PREFIX_SIZE])
    run = 0
    for byte in inner[_INNER_PREFIX_SIZE:]:
        escaped.append(byte)
        if byte == _ESCAPE_BYTE:
            run += 1
            if run == _ESCAPE_RUN:
                escaped.append(_STUFF_BYTE)
                run = 0
        else:
            run = 0
    return bytes(escaped)


def _unescape(escaped: bytes) -> bytes:
    if len(escaped) < _INNER_PREFIX_SIZE or not escaped.startswith(_INNER_PREFIX):
        raise ConsoleProtocolError("invalid inner header")
    logical = bytearray(escaped[:_INNER_PREFIX_SIZE])
    run = 0
    index = _INNER_PREFIX_SIZE
    limit = len(escaped)
    while index < limit:
        byte = escaped[index]
        logical.append(byte)
        index += 1
        if byte == _ESCAPE_BYTE:
            run += 1
            if run == _ESCAPE_RUN:
                if index >= limit or escaped[index] != _STUFF_BYTE:
                    raise ConsoleProtocolError("missing escape byte")
                index += 1
                run = 0
        else:
            run = 0
    return bytes(logical)


def _decode_inner(escaped: bytes) -> tuple[At5Header, bytes]:
    logical = _unescape(escaped)
    if len(logical) < _MINIMUM_INNER_SIZE or len(logical) > _MAXIMUM_FRAME_SIZE:
        raise ConsoleProtocolError("invalid logical frame length")
    header_tail = _DECODER.header_length - _OUTER_SIZE
    outer = _OUTER_PREFIX + b"\x00\x00" + struct.pack("!HH", len(logical), len(logical))
    try:
        decoded = _DECODER.decode(outer + logical[:header_tail])
        decoded.assert_complete()
    except DecodeError as err:
        raise ConsoleProtocolError("invalid logical header") from err
    rest = logical[header_tail:]
    message_length = decoded.header.message_length
    if len(rest) != message_length + _CRC.checksum_length:
        raise ConsoleProtocolError("payload length does not match header")
    payload = rest[:message_length]
    checksum = rest[message_length:]
    if not _CRC.validate(decoded.checksum_data + payload, checksum):
        raise ConsoleChecksumError("checksum mismatch")
    return decoded.header, payload


async def _read_frame(reader: asyncio.StreamReader) -> tuple[At5Header, bytes]:
    outer = await reader.readexactly(_OUTER_SIZE)
    prefix = outer[:4]
    zeros = outer[4:6]
    length_a, length_b = struct.unpack_from("!HH", outer, 6)
    if prefix != _OUTER_PREFIX or zeros != b"\x00\x00" or length_a != length_b:
        raise ConsoleProtocolError("invalid frame header")
    if length_a < _MINIMUM_INNER_SIZE or length_a > _MAXIMUM_FRAME_SIZE:
        raise ConsoleProtocolError("invalid frame length")
    escaped = await reader.readexactly(length_a)
    return _decode_inner(escaped)


def _answers(header: At5Header, request: At5Header) -> bool:
    return (
        header.to_address == request.from_address
        and header.from_address == request.to_address
        and header.packet_id == request.packet_id
        and header.message_id == request.message_id
    )


async def _read_matching(
    reader: asyncio.StreamReader,
    request: At5Header,
    *,
    reply_prefix: bytes,
    broadcast_prefix: bytes | None,
) -> tuple[bytes, bytes | None]:
    broadcast: bytes | None = None
    while True:
        header, payload = await _read_frame(reader)
        if _answers(header, request) and payload.startswith(reply_prefix):
            return payload, broadcast
        if (
            broadcast_prefix is not None
            and header.to_address == _ADDRESS_ALL_CLIENTS
            and header.from_address == ADDRESS_AIRTOUCH_EXTENDED
            and header.message_id == EXTENDED_MESSAGE_ID
            and payload.startswith(broadcast_prefix)
        ):
            broadcast = payload[len(broadcast_prefix) :]
            continue
        _LOGGER.debug(
            "Skipping console frame id=%s packet=%s",
            header.message_id,
            header.packet_id,
        )


async def _read_pair(
    reader: asyncio.StreamReader,
    request: At5Header,
    *,
    base_prefix: bytes,
    extension_prefix: bytes,
) -> tuple[bytes, bytes]:
    bodies: dict[bytes, bytes] = {}
    while base_prefix not in bodies or extension_prefix not in bodies:
        header, payload = await _read_frame(reader)
        prefix = next(
            (p for p in (base_prefix, extension_prefix) if payload.startswith(p)),
            None,
        )
        if prefix is None or not _answers(header, request):
            _LOGGER.debug(
                "Skipping console frame id=%s packet=%s",
                header.message_id,
                header.packet_id,
            )
            continue
        if prefix in bodies:
            raise ConsoleProtocolError("duplicate reply")
        bodies[prefix] = payload[len(prefix) :]
    return bodies[base_prefix], bodies[extension_prefix]


async def _close_writer_preserving_cancellation(writer: asyncio.StreamWriter) -> None:
    writer.close()
    task = asyncio.current_task()
    released = task.cancelling() if task is not None else 0
    if task is not None:
        for _ in range(released):
            task.uncancel()
    try:
        await asyncio.wait_for(writer.wait_closed(), _CLOSE_TIMEOUT)
    except (TimeoutError, OSError):
        transport = writer.transport
        if transport is not None:
            transport.abort()
    finally:
        if task is not None:
            for _ in range(released):
                task.cancel()
