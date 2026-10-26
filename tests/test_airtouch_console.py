from __future__ import annotations

import asyncio
import contextlib
import struct
import unittest
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Callable

from pyairtouch.at5.comms.hdr import At5Header, HeaderDecoder, HeaderEncoder
from pyairtouch.comms.crc16 import Crc16Modbus

from custom_components.airtouch import console

_OUTER = b"\x55\x55\x55\xab"
_ENCODER = HeaderEncoder()
_DECODER = HeaderDecoder()
_CRC = Crc16Modbus()
_CONSOLE = 0x80
_EXTENDED = 0x90
_CLIENT = 0xB0
_ALL_CLIENTS = 0xFF
_CONTROL = 0xC0
_EXTENDED_MESSAGE = 0x1F
_STATUS_QUERY = bytes.fromhex("5d00000000000000")
_STATUS_REPORT = bytes.fromhex("5d000002000300010400020002")
_OTHER_REPORT = bytes.fromhex("45000002000000000102")
_NAMES = bytes.fromhex("ff04")
_TRIGGER = bytes.fromhex("5c000008000000000000000100000000")
_PROGRAMS = bytes.fromhex("ff41")
_PROGRAMS_EXTENSION = bytes.fromhex("ff45")
_BASE_BODY = bytes.fromhex("0000000000000102005555555510")
_EXTENSION_BODY = bytes.fromhex("55555555555555")


def _oracle_escape(inner: bytes) -> bytes:
    out = bytearray(inner[:4])
    run = 0
    for byte in inner[4:]:
        out.append(byte)
        if byte == 0x55:
            run += 1
            if run == 3:
                out.append(0)
                run = 0
        else:
            run = 0
    return bytes(out)


def _outer(escaped: bytes) -> bytes:
    return _OUTER + b"\x00\x00" + struct.pack("!HH", len(escaped), len(escaped))


def _logical(
    *,
    to_address: int,
    from_address: int,
    packet_id: int,
    message_id: int,
    payload: bytes,
) -> bytes:
    header = At5Header(
        to_address=to_address,
        from_address=from_address,
        packet_id=packet_id,
        message_id=message_id,
        message_length=len(payload),
    )
    encoded = _ENCODER.encode(header)
    crc = _CRC.calculate(encoded.checksum_data + payload)
    return encoded.header_bytes[10:] + payload + crc


def _frame(
    *,
    to_address: int,
    from_address: int,
    packet_id: int,
    message_id: int,
    payload: bytes,
) -> bytes:
    escaped = _oracle_escape(
        _logical(
            to_address=to_address,
            from_address=from_address,
            packet_id=packet_id,
            message_id=message_id,
            payload=payload,
        )
    )
    return _outer(escaped) + escaped


def _oracle_decode(frame: bytes) -> tuple[At5Header, bytes]:
    length = struct.unpack_from("!H", frame, 6)[0]
    escaped = frame[10 : 10 + length]
    logical = bytearray(escaped[:4])
    run = 0
    index = 4
    while index < len(escaped):
        logical.append(escaped[index])
        index += 1
        if logical[-1] == 0x55:
            run += 1
            if run == 3:
                if index >= len(escaped) or escaped[index] != 0:
                    raise AssertionError("oracle expected a stuffed zero")
                index += 1
                run = 0
        else:
            run = 0
    restored = _outer(bytes(logical)) + bytes(logical)
    decoded = _DECODER.decode(restored[: _DECODER.header_length])
    decoded.assert_complete()
    rest = bytes(logical)[10:]
    return decoded.header, rest[: decoded.header.message_length]


def _report(packet_id: int, payload: bytes, *, from_address: int = _CONSOLE) -> bytes:
    return _frame(
        to_address=_CLIENT,
        from_address=from_address,
        packet_id=packet_id,
        message_id=_CONTROL,
        payload=payload,
    )


def _broadcast(
    packet_id: int,
    payload: bytes,
    *,
    to_address: int = _ALL_CLIENTS,
    from_address: int = _EXTENDED,
    message_id: int = _EXTENDED_MESSAGE,
) -> bytes:
    return _frame(
        to_address=to_address,
        from_address=from_address,
        packet_id=packet_id,
        message_id=message_id,
        payload=payload,
    )


def _extended_reply(
    packet_id: int,
    payload: bytes,
    *,
    to_address: int = _CLIENT,
    from_address: int = _EXTENDED,
    message_id: int = _EXTENDED_MESSAGE,
) -> bytes:
    return _frame(
        to_address=to_address,
        from_address=from_address,
        packet_id=packet_id,
        message_id=message_id,
        payload=payload,
    )


def _program_pair(header: At5Header, payload: bytes) -> list[bytes]:
    if payload != _PROGRAMS:
        return _status_only(header, payload)
    return [
        _extended_reply(header.packet_id, _PROGRAMS + _BASE_BODY),
        _extended_reply(header.packet_id, _PROGRAMS_EXTENSION + _EXTENSION_BODY),
    ]


async def _read_raw(reader: asyncio.StreamReader) -> bytes:
    outer = await reader.readexactly(10)
    length = struct.unpack_from("!H", outer, 6)[0]
    return outer + await reader.readexactly(length)


def _status_only(header: At5Header, payload: bytes) -> list[bytes]:
    if payload[:1] == b"\x5c":
        return []
    return [_report(header.packet_id, _STATUS_REPORT)]


async def _stalled_drain(writer: asyncio.StreamWriter) -> None:
    del writer
    await asyncio.sleep(10)


async def _reset_drain(writer: asyncio.StreamWriter) -> None:
    del writer
    raise ConnectionResetError("reset while draining")


class _ConsoleServerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.raw: list[bytes] = []
        self.received: list[tuple[At5Header, bytes]] = []
        self.after_frame: list[bytes] = []
        self.replies: Callable[[At5Header, bytes], list[bytes]] = _status_only
        self.bytewise = False
        self.close_after_reply = False
        self.reply_gap = 0.0
        self.handled = asyncio.Event()
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sockets = self.server.sockets
        if not sockets:
            raise RuntimeError("test server did not bind a socket")
        self.port = sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    def _client(self, deadline: float = 2.0) -> console.ConsoleClient:
        return console.ConsoleClient("127.0.0.1", self.port, deadline=deadline)

    async def _client_closes(self, count: int) -> list[bytes]:
        for _ in range(100):
            if len(self.after_frame) >= count:
                break
            await asyncio.sleep(0.01)
        return self.after_frame

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await _read_raw(reader)
            self.raw.append(raw)
            header, payload = _oracle_decode(raw)
            self.received.append((header, payload))
            self.handled.set()
            frames = self.replies(header, payload)
            data = b"".join(frames)
            if data and self.bytewise:
                for index in range(len(data)):
                    writer.write(data[index : index + 1])
                    await writer.drain()
            elif self.reply_gap:
                for frame in frames:
                    await asyncio.sleep(self.reply_gap)
                    writer.write(frame)
                    await writer.drain()
            elif data:
                writer.write(data)
                await writer.drain()
            if not self.close_after_reply:
                self.after_frame.append(await asyncio.wait_for(reader.read(1), 2))
        except (asyncio.IncompleteReadError, ConnectionError, OSError, TimeoutError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


class ConsoleStatusAndSendTests(_ConsoleServerTestCase):
    async def test_status_query_frame_is_exact(self) -> None:
        report, names = await self._client().query_status(
            _STATUS_QUERY, broadcast_prefix=_NAMES
        )
        self.assertEqual(report, _STATUS_REPORT)
        self.assertIsNone(names)
        self.assertEqual(
            self.raw,
            [
                _frame(
                    to_address=_CONSOLE,
                    from_address=_CLIENT,
                    packet_id=1,
                    message_id=_CONTROL,
                    payload=bytes.fromhex("5d00000000000000"),
                )
            ],
        )
        self.assertEqual(await self._client_closes(1), [b""])

    async def test_last_broadcast_before_the_report_is_returned(self) -> None:
        escaped_names = b"\x01\x55\x55\x55\x55\x02"

        def replies(header: At5Header, payload: bytes) -> list[bytes]:
            del payload
            packet = header.packet_id
            return [
                _broadcast(180, _NAMES + b"first"),
                _report(packet, _OTHER_REPORT),
                _report(packet + 1, _STATUS_REPORT),
                _report(packet, _STATUS_REPORT, from_address=_EXTENDED),
                _broadcast(181, _NAMES + escaped_names),
                _broadcast(182, _NAMES + b"direct", to_address=_CLIENT),
                _broadcast(183, bytes.fromhex("ff18") + b"other"),
                _broadcast(184, _NAMES + b"wrong source", from_address=_CONSOLE),
                _broadcast(185, _NAMES + b"wrong message", message_id=_CONTROL),
                _report(packet, _STATUS_REPORT),
                _broadcast(186, _NAMES + b"after the report"),
            ]

        self.replies = replies
        for bytewise in (False, True):
            with self.subTest(bytewise=bytewise):
                self.bytewise = bytewise
                report, names = await self._client().query_status(
                    _STATUS_QUERY, broadcast_prefix=_NAMES
                )
                self.assertEqual(report, _STATUS_REPORT)
                self.assertEqual(names, escaped_names)
        self.assertIn(b"\x55\x55\x55\x00", replies(self.received[0][0], b"")[4])

    async def test_report_without_an_earlier_broadcast_has_no_names(self) -> None:
        self.replies = lambda header, _payload: [
            _report(header.packet_id, _STATUS_REPORT),
            _broadcast(200, _NAMES + b"late"),
        ]
        report, names = await self._client().query_status(
            _STATUS_QUERY, broadcast_prefix=_NAMES
        )
        self.assertEqual(report, _STATUS_REPORT)
        self.assertIsNone(names)

    async def test_other_subcommand_answers_a_setter_but_not_a_status_query(
        self,
    ) -> None:
        self.close_after_reply = True
        self.replies = lambda header, _payload: [
            _report(header.packet_id, _OTHER_REPORT)
        ]
        client = self._client()
        with self.assertRaisesRegex(console.ConsoleProtocolError, "closed"):
            await client.query_status(_STATUS_QUERY, broadcast_prefix=_NAMES)
        setter = bytes.fromhex("4400000200000000aabb")
        self.assertEqual(await client.exchange_control(setter), _OTHER_REPORT)
        self.assertEqual([payload for _, payload in self.received][1], setter)

    async def test_corrupt_broadcasts_fail_the_status_query(self) -> None:
        good = _broadcast(180, _NAMES + b"\x55\x55\x55\x55")
        bad_crc = bytearray(good)
        bad_crc[-1] ^= 0xFF
        logical = _logical(
            to_address=_ALL_CLIENTS,
            from_address=_EXTENDED,
            packet_id=180,
            message_id=_EXTENDED_MESSAGE,
            payload=_NAMES + b"\x55\x55\x55\x55",
        )
        missing_stuffing = _outer(logical) + logical
        bad_outer = _OUTER + b"\x00\x00" + struct.pack("!HH", 20, 21)
        cases: list[tuple[bytes, type[Exception]]] = [
            (bytes(bad_crc), console.ConsoleChecksumError),
            (missing_stuffing, console.ConsoleProtocolError),
            (bad_outer, console.ConsoleProtocolError),
        ]
        client = self._client()
        for corrupt, error in cases:
            with self.subTest(error=error.__name__, frame=corrupt[:12].hex()):

                def replies(
                    header: At5Header, payload: bytes, bad: bytes = corrupt
                ) -> list[bytes]:
                    del payload
                    return [bad, _report(header.packet_id, _STATUS_REPORT)]

                self.replies = replies
                with self.assertRaises(error):
                    await client.query_status(_STATUS_QUERY, broadcast_prefix=_NAMES)
        self.replies = _status_only
        report, _ = await client.query_status(_STATUS_QUERY, broadcast_prefix=_NAMES)
        self.assertEqual(report, _STATUS_REPORT)

    async def test_status_timeout_and_cancellation_close_and_release(self) -> None:
        self.replies = lambda _header, _payload: []
        client = self._client(deadline=0.3)
        with self.assertRaises(TimeoutError):
            await client.query_status(_STATUS_QUERY, broadcast_prefix=_NAMES)
        self.handled.clear()
        task = asyncio.create_task(
            client.query_status(_STATUS_QUERY, broadcast_prefix=_NAMES)
        )
        await asyncio.wait_for(self.handled.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.replies = _status_only
        report, _ = await client.query_status(_STATUS_QUERY, broadcast_prefix=_NAMES)
        self.assertEqual(report, _STATUS_REPORT)
        self.assertEqual(await self._client_closes(3), [b"", b"", b""])

    async def test_send_control_returns_after_submission_without_a_reply(
        self,
    ) -> None:
        client = self._client(deadline=5)
        await client.query_status(_STATUS_QUERY, broadcast_prefix=_NAMES)
        await asyncio.wait_for(client.send_control(_TRIGGER), 1)
        await client.query_status(_STATUS_QUERY, broadcast_prefix=_NAMES)
        self.assertEqual(
            self.raw[1],
            _frame(
                to_address=_CONSOLE,
                from_address=_CLIENT,
                packet_id=2,
                message_id=_CONTROL,
                payload=bytes.fromhex("5c000008000000000000000100000000"),
            ),
        )
        self.assertEqual([header.packet_id for header, _ in self.received], [1, 2, 3])
        self.assertEqual(await self._client_closes(3), [b"", b"", b""])

    async def test_refused_connection_is_not_an_uncertain_send(self) -> None:
        closed = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = closed.sockets[0].getsockname()[1]
        closed.close()
        await closed.wait_closed()
        client = console.ConsoleClient("127.0.0.1", port, deadline=1)
        with self.assertRaises(OSError) as raised:
            await client.send_control(_TRIGGER)
        self.assertNotIsInstance(raised.exception, console.ConsoleSendUncertainError)
        self.assertEqual(self.received, [])

    async def test_failure_after_writing_is_an_uncertain_send(self) -> None:
        for name, drain in (("reset", _reset_drain), ("stalled", _stalled_drain)):
            with self.subTest(drain=name):
                self.received.clear()
                self.handled.clear()
                client = self._client(deadline=0.3)
                with (
                    patch.object(asyncio.StreamWriter, "drain", drain),
                    self.assertRaises(console.ConsoleSendUncertainError),
                ):
                    await client.send_control(_TRIGGER)
                await asyncio.wait_for(self.handled.wait(), 1)
                self.assertEqual([payload for _, payload in self.received], [_TRIGGER])
                report, _ = await client.query_status(
                    _STATUS_QUERY, broadcast_prefix=_NAMES
                )
                self.assertEqual(report, _STATUS_REPORT)

    async def test_cancelled_send_closes_and_releases_the_lock(self) -> None:
        client = self._client(deadline=5)
        with patch.object(asyncio.StreamWriter, "drain", _stalled_drain):
            task = asyncio.create_task(client.send_control(_TRIGGER))
            await asyncio.wait_for(self.handled.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        report, _ = await client.query_status(_STATUS_QUERY, broadcast_prefix=_NAMES)
        self.assertEqual(report, _STATUS_REPORT)
        self.assertEqual(
            [payload for _, payload in self.received], [_TRIGGER, _STATUS_QUERY]
        )
        self.assertEqual(await self._client_closes(2), [b"", b""])

    async def test_remote_close_after_receipt_sends_one_frame(self) -> None:
        self.close_after_reply = True
        client = self._client()
        try:
            await client.send_control(_TRIGGER)
        except console.ConsoleSendUncertainError:
            outcome = "uncertain"
        else:
            outcome = "submitted"
        self.assertIn(outcome, {"submitted", "uncertain"})
        await asyncio.wait_for(self.handled.wait(), 1)
        self.assertEqual([payload for _, payload in self.received], [_TRIGGER])


class ConsolePairedReportTests(_ConsoleServerTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.replies = _program_pair

    async def _pair(self, client: console.ConsoleClient) -> tuple[bytes, bytes]:
        return await client.request_with_extension(
            _PROGRAMS, extension_prefix=_PROGRAMS_EXTENSION
        )

    async def test_one_query_frame_returns_both_bodies(self) -> None:
        client = self._client()
        self.assertEqual(await self._pair(client), (_BASE_BODY, _EXTENSION_BODY))
        self.assertEqual(
            self.raw,
            [
                _frame(
                    to_address=_EXTENDED,
                    from_address=_CLIENT,
                    packet_id=1,
                    message_id=_EXTENDED_MESSAGE,
                    payload=bytes.fromhex("ff41"),
                )
            ],
        )
        await client.query_status(_STATUS_QUERY, broadcast_prefix=_NAMES)
        self.assertEqual(await self._pair(client), (_BASE_BODY, _EXTENSION_BODY))
        self.assertEqual([header.packet_id for header, _ in self.received], [1, 2, 3])
        self.assertEqual(
            [payload for _, payload in self.received],
            [_PROGRAMS, _STATUS_QUERY, _PROGRAMS],
        )
        self.assertEqual(await self._client_closes(3), [b"", b"", b""])

    async def test_either_order_fragmented_coalesced_and_escaped(self) -> None:
        def reversed_pair(header: At5Header, payload: bytes) -> list[bytes]:
            return list(reversed(_program_pair(header, payload)))

        client = self._client()
        for replies in (_program_pair, reversed_pair):
            for bytewise in (False, True):
                with self.subTest(order=replies.__name__, bytewise=bytewise):
                    self.replies = replies
                    self.bytewise = bytewise
                    self.assertEqual(
                        await self._pair(client), (_BASE_BODY, _EXTENSION_BODY)
                    )
        self.assertIn(
            b"\x55\x55\x55\x00", _program_pair(self.received[0][0], _PROGRAMS)[1]
        )

    async def test_uncorrelated_frames_are_skipped(self) -> None:
        decoy = b"\x00" * 8

        def replies(header: At5Header, payload: bytes) -> list[bytes]:
            del payload
            packet = header.packet_id
            return [
                _extended_reply(packet, _PROGRAMS + decoy, to_address=_ALL_CLIENTS),
                _extended_reply(
                    packet, _PROGRAMS_EXTENSION + decoy, to_address=_ALL_CLIENTS
                ),
                _extended_reply(packet, _PROGRAMS + decoy, to_address=0xB1),
                _extended_reply(packet + 1, _PROGRAMS + decoy),
                _extended_reply(
                    packet, _PROGRAMS_EXTENSION + decoy, from_address=_CONSOLE
                ),
                _extended_reply(packet, _PROGRAMS + decoy, message_id=_CONTROL),
                _extended_reply(packet, bytes.fromhex("ff18") + decoy),
                _extended_reply(packet, _PROGRAMS_EXTENSION + _EXTENSION_BODY),
                _extended_reply(packet - 1, _PROGRAMS_EXTENSION + decoy),
                _extended_reply(packet, _PROGRAMS + _BASE_BODY),
                _extended_reply(packet, _PROGRAMS + decoy),
            ]

        self.replies = replies
        self.assertEqual(
            await self._pair(self._client()), (_BASE_BODY, _EXTENSION_BODY)
        )

    async def test_duplicate_reply_fails_even_when_identical(self) -> None:
        client = self._client()
        for first in (_PROGRAMS + _BASE_BODY, _PROGRAMS_EXTENSION + _EXTENSION_BODY):
            with self.subTest(prefix=first[:2].hex()):

                def replies(
                    header: At5Header, payload: bytes, repeated: bytes = first
                ) -> list[bytes]:
                    return [
                        _extended_reply(header.packet_id, repeated),
                        _extended_reply(header.packet_id, repeated),
                        *_program_pair(header, payload),
                    ]

                self.replies = replies
                with self.assertRaisesRegex(console.ConsoleProtocolError, "duplicate"):
                    await self._pair(client)
        self.replies = _program_pair
        self.assertEqual(await self._pair(client), (_BASE_BODY, _EXTENSION_BODY))

    async def test_missing_partner_eof_and_corrupt_frames_fail(self) -> None:
        client = self._client(deadline=0.3)
        self.replies = lambda header, payload: _program_pair(header, payload)[:1]
        with self.assertRaises(TimeoutError):
            await self._pair(client)
        self.close_after_reply = True
        with self.assertRaisesRegex(console.ConsoleProtocolError, "closed"):
            await self._pair(client)
        self.close_after_reply = False

        def bad_crc(header: At5Header, payload: bytes) -> list[bytes]:
            base, extension = _program_pair(header, payload)
            corrupt = bytearray(extension)
            corrupt[-1] ^= 0xFF
            return [base, bytes(corrupt)]

        self.replies = bad_crc
        with self.assertRaises(console.ConsoleChecksumError):
            await self._pair(client)
        self.replies = _program_pair
        self.assertEqual(await self._pair(client), (_BASE_BODY, _EXTENSION_BODY))

    async def test_one_deadline_covers_both_replies(self) -> None:
        client = self._client(deadline=0.5)
        self.reply_gap = 0.1
        self.assertEqual(await self._pair(client), (_BASE_BODY, _EXTENSION_BODY))
        self.reply_gap = 0.3
        with self.assertRaises(TimeoutError):
            await self._pair(client)

    async def test_timeout_and_cancellation_close_and_release(self) -> None:
        self.replies = lambda _header, _payload: []
        client = self._client(deadline=0.3)
        with self.assertRaises(TimeoutError):
            await self._pair(client)
        self.handled.clear()
        task = asyncio.create_task(self._pair(client))
        await asyncio.wait_for(self.handled.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.replies = _program_pair
        self.assertEqual(await self._pair(client), (_BASE_BODY, _EXTENSION_BODY))
        self.assertEqual(await self._client_closes(3), [b"", b"", b""])


if __name__ == "__main__":
    unittest.main()
