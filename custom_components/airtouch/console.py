"""Short-lived AirTouch 5 console exchanges.

The console inserts a zero after every three 0x55 bytes that follow the inner
header prefix, including the checksum. pyairtouch encodes the logical header
and checksum. This module adds that escape and reads one matching frame before
the deadline. Extended history uses message 0x1F. Preference settings use the
same framing with control message 0xC0.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Final

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

_ENCODER: Final = HeaderEncoder()
_DECODER: Final = HeaderDecoder()
_CRC: Final = Crc16Modbus()


class ConsoleProtocolError(Exception):
    """A console frame was truncated, misframed, or not the requested report."""


class ConsoleChecksumError(ConsoleProtocolError):
    """A framed message failed the logical CRC."""


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

    async def request(self, payload: bytes) -> bytes:
        """Send one extended request and return the bytes after its echo.

        Unrelated valid frames are ignored until the deadline. A bad header
        or checksum ends the exchange. The connection lock is released on
        cancellation.
        """
        return await self._exchange(
            to_address=ADDRESS_AIRTOUCH_EXTENDED,
            message_id=EXTENDED_MESSAGE_ID,
            payload=payload,
            echo_prefix=payload,
        )

    async def exchange_control(self, payload: bytes) -> bytes:
        """Send one 0xC0 message and return the matching reply payload.

        History replies repeat the request. A control reply keeps the packet
        id and swaps the console and client addresses, and its subheader
        lengths replace the request, so the first matching frame is the reply.
        """
        return await self._exchange(
            to_address=ADDRESS_AIRTOUCH,
            message_id=CONTROL_MESSAGE_ID,
            payload=payload,
            echo_prefix=None,
        )

    async def _exchange(
        self,
        *,
        to_address: int,
        message_id: int,
        payload: bytes,
        echo_prefix: bytes | None,
    ) -> bytes:
        async with self._lock:
            packet_id = self._packet_id
            self._packet_id = 1 if packet_id == _PACKET_ID_MAX else packet_id + 1
            header = At5Header(
                to_address=to_address,
                from_address=ADDRESS_CLIENT,
                packet_id=packet_id,
                message_id=message_id,
                message_length=len(payload),
            )
            frame = encode_frame(header, payload)
            writer: asyncio.StreamWriter | None = None
            try:
                async with asyncio.timeout(self._deadline):
                    reader, writer = await asyncio.open_connection(
                        self._host, self._port
                    )
                    writer.write(frame)
                    await writer.drain()
                    try:
                        return await _read_matching(
                            reader,
                            packet_id=packet_id,
                            message_id=message_id,
                            from_address=to_address,
                            echo_prefix=echo_prefix,
                        )
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


async def _read_matching(
    reader: asyncio.StreamReader,
    *,
    packet_id: int,
    message_id: int,
    from_address: int,
    echo_prefix: bytes | None,
) -> bytes:
    while True:
        header, payload = await _read_frame(reader)
        if (
            header.to_address == ADDRESS_CLIENT
            and header.from_address == from_address
            and header.packet_id == packet_id
            and header.message_id == message_id
            and (echo_prefix is None or payload.startswith(echo_prefix))
        ):
            if echo_prefix is None:
                return payload
            return payload[len(echo_prefix) :]
        _LOGGER.debug(
            "Skipping console frame id=%s packet=%s",
            header.message_id,
            header.packet_id,
        )


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
