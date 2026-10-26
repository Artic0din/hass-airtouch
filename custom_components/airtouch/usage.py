"""AirTouch 5 usage history.

Records are frozen values in minutes. A missing day or hour stays unknown.
An air conditioner left out of a present record ran for zero minutes, which
is how the console omits an idle unit. A failed parse produces no zeros.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal
from zoneinfo import ZoneInfo

from homeassistant.config_entries import current_entry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pyairtouch.at5.api import DEFAULT_PORT_NUMBER

if TYPE_CHECKING:
    from asyncio import TimerHandle

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from pyairtouch import AirTouch

from .console import DEFAULT_DEADLINE, ConsoleClient, ConsoleProtocolError

_LOGGER = logging.getLogger(__name__)

DAILY_RESOLUTION_MINUTES: Final = 6
HOURLY_RESOLUTION_MINUTES: Final = 1
_UPDATE_INTERVAL: Final = 15
_DATE_CHANGE_ATTEMPTS: Final = 2
_AC_MARKER: Final = 0x80
_ZONE_MARKER: Final = 0xC0
_MARKER_MASK: Final = 0xC0
_ID_MASK: Final = 0x3F
_AC_RECORD_KIND: Final = 2
_AC_SHORT_SIZE: Final = 4
_AC_LONG_SIZE: Final = 12
_AC_COUNTERS: Final = 9
_ZONE_RECORD_SIZE: Final = 2
_TRAILER_SIZE: Final = 8
_TRAILER_KIND: Final = 6
_TRAILER_FLAG: Final = 1
_DAY_MIN: Final = 1
_DAY_MAX: Final = 31
_HOUR_MIN: Final = 0
_HOUR_MAX: Final = 23
_ABSENT_TEMPERATURE: Final = 2000
_TEMPERATURE_OFFSET: Final = 500
_SETPOINT_OFFSET: Final = 100
_TEMPERATURE_SCALE: Final = 10
_SOLUTION_CAP: Final = 2
_NODE_BUDGET: Final = 50000
_ON_COUNTER: Final = 0
_DUPLICATE_ON_COUNTER: Final = 8

_MODE_FIELDS: Final = (
    "on_minutes",
    "away_minutes",
    "sleep_minutes",
    "cool_minutes",
    "heat_minutes",
    "fan_minutes",
    "dry_minutes",
    "weather_adapt_minutes",
)


class UsageParseError(Exception):
    """The usage payload is malformed or has more than one structural parse."""


@dataclass(frozen=True, slots=True)
class AcUsage:
    """Run time and average setpoint for one air conditioner in one record."""

    ac_id: int
    on_minutes: int
    away_minutes: int
    sleep_minutes: int
    cool_minutes: int
    heat_minutes: int
    fan_minutes: int
    dry_minutes: int
    weather_adapt_minutes: int
    average_setpoint_c: float | None

    @staticmethod
    def omitted(ac_id: int) -> AcUsage:
        """An air conditioner the console left out of a present record."""
        return AcUsage(ac_id, 0, 0, 0, 0, 0, 0, 0, 0, None)


@dataclass(frozen=True, slots=True)
class ZoneUsage:
    """Damper-open run time for one zone in one record."""

    zone_id: int
    on_minutes: int


@dataclass(frozen=True, slots=True)
class UsageBucket:
    """One day or one hour from a usage report."""

    index: int
    air_conditioners: tuple[AcUsage, ...]
    zones: tuple[ZoneUsage, ...]
    minimum_temperature_c: float | None
    maximum_temperature_c: float | None


@dataclass(frozen=True, slots=True)
class UsageHistory:
    """The buckets from one daily or hourly report."""

    resolution_minutes: int
    buckets: tuple[UsageBucket, ...]

    def bucket(self, index: int) -> UsageBucket | None:
        """Return the record for this day or hour, if the console stored one."""
        for item in self.buckets:
            if item.index == index:
                return item
        return None

    def ac_usage(self, ac_id: int, index: int) -> AcUsage | None:
        """Return run time, zero when the unit was omitted, or none if no record."""
        item = self.bucket(index)
        if item is None:
            return None
        for usage in item.air_conditioners:
            if usage.ac_id == ac_id:
                return usage
        return AcUsage.omitted(ac_id)

    def zone_on_minutes(self, zone_id: int, index: int) -> int | None:
        """Return zone run time, or none when that record or zone is absent."""
        item = self.bucket(index)
        if item is None:
            return None
        for zone in item.zones:
            if zone.zone_id == zone_id:
                return zone.on_minutes
        return None


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """Daily and hourly reports for one Home Assistant local date."""

    local_date: date
    daily: UsageHistory
    hourly: UsageHistory

    def ac_today_minutes(self, ac_id: int, *, now: datetime) -> int | None:
        """Return today's daily on time when this snapshot is still for that date."""
        if now.date() != self.local_date:
            return None
        usage = self.daily.ac_usage(ac_id, self.local_date.day)
        if usage is None:
            return None
        return usage.on_minutes

    def zone_today_minutes(self, zone_id: int, *, now: datetime) -> int | None:
        """Return today's daily zone on time when this snapshot is still current."""
        if now.date() != self.local_date:
            return None
        return self.daily.zone_on_minutes(zone_id, self.local_date.day)

    def ac_attributes(self, ac_id: int, *, now: datetime) -> dict[str, Any] | None:
        """Serialize history visible for this local date."""
        if now.date() != self.local_date:
            return None
        today_usage = self.daily.ac_usage(ac_id, self.local_date.day)
        return {
            "local_date": self.local_date.isoformat(),
            "daily_minute_resolution": DAILY_RESOLUTION_MINUTES,
            "hourly_minute_resolution": HOURLY_RESOLUTION_MINUTES,
            "daily_runtime_is_approximate": True,
            "mode_breakdown": None if today_usage is None else _ac_fields(today_usage),
            "daily": [
                _dated_ac_row(self.daily, ac_id, day, label="day")
                for day in range(1, now.day + 1)
            ],
            "hourly": [
                _dated_ac_row(self.hourly, ac_id, hour, label="hour")
                for hour in range(now.hour + 1)
            ],
        }

    def zone_attributes(self, zone_id: int, *, now: datetime) -> dict[str, Any] | None:
        """Serialize per-zone history visible for this local date."""
        if now.date() != self.local_date:
            return None
        return {
            "local_date": self.local_date.isoformat(),
            "daily_minute_resolution": DAILY_RESOLUTION_MINUTES,
            "hourly_minute_resolution": HOURLY_RESOLUTION_MINUTES,
            "daily_runtime_is_approximate": True,
            "daily": [
                {
                    "day": day,
                    "on_minutes": self.daily.zone_on_minutes(zone_id, day),
                }
                for day in range(1, now.day + 1)
            ],
            "hourly": [
                {
                    "hour": hour,
                    "on_minutes": self.hourly.zone_on_minutes(zone_id, hour),
                }
                for hour in range(now.hour + 1)
            ],
        }


def local_now(hass: HomeAssistant) -> datetime:
    """Return the current time in the Home Assistant time zone."""
    return datetime.now(ZoneInfo(hass.config.time_zone))


def daily_request(day: date) -> bytes:
    """Build the daily report request.

    The console reads two ASCII year digits and treats them as 20xx.
    """
    return bytes((0xFF, 0x81)) + f"{day:%y%m}".encode("ascii")


def hourly_request(day: date) -> bytes:
    """Build the hourly report request for one local date."""
    return bytes((0xFF, 0x82)) + f"{day:%y%m%d}".encode("ascii")


def parse_usage_history(body: bytes, *, resolution_minutes: int) -> UsageHistory:
    """Parse the bytes that follow the echoed request prefix."""
    if resolution_minutes not in (
        DAILY_RESOLUTION_MINUTES,
        HOURLY_RESOLUTION_MINUTES,
    ):
        raise ValueError("resolution_minutes must be 1 or 6")
    hourly = resolution_minutes == HOURLY_RESOLUTION_MINUTES
    buckets = _parse_buckets(body, resolution_minutes=resolution_minutes, hourly=hourly)
    return UsageHistory(resolution_minutes=resolution_minutes, buckets=buckets)


class UsageCoordinator(DataUpdateCoordinator[UsageSnapshot]):
    """Poll both usage reports on a short-lived console connection."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        airtouch: AirTouch,
        *,
        port: int = DEFAULT_PORT_NUMBER,
        deadline: float = DEFAULT_DEADLINE,
    ) -> None:
        self._client = ConsoleClient(airtouch.host, port, deadline=deadline)
        self._midnight_timer: TimerHandle | None = None
        token = current_entry.set(entry)
        try:
            super().__init__(
                hass,
                _LOGGER,
                name="AirTouch usage",
                update_interval=timedelta(minutes=_UPDATE_INTERVAL),
            )
        finally:
            current_entry.reset(token)

    async def async_shutdown(self) -> None:
        """Cancel the history expiry and the scheduled refresh."""
        if self._midnight_timer is not None:
            self._midnight_timer.cancel()
        await super().async_shutdown()

    async def _async_update_data(self) -> UsageSnapshot:
        for _ in range(_DATE_CHANGE_ATTEMPTS):
            try:
                today = local_now(self.hass).date()
            except (KeyError, ValueError) as err:
                raise UpdateFailed("Home Assistant time zone is not usable") from err
            try:
                daily_body = await self._client.request(daily_request(today))
                hourly_body = await self._client.request(hourly_request(today))
                daily = parse_usage_history(
                    daily_body, resolution_minutes=DAILY_RESOLUTION_MINUTES
                )
                hourly = parse_usage_history(
                    hourly_body, resolution_minutes=HOURLY_RESOLUTION_MINUTES
                )
            except (
                ConsoleProtocolError,
                UsageParseError,
                OSError,
                TimeoutError,
            ) as err:
                raise UpdateFailed(str(err)) from err
            try:
                current = local_now(self.hass)
            except (KeyError, ValueError) as err:
                raise UpdateFailed("Home Assistant time zone is not usable") from err
            if current.date() == today:
                if self._midnight_timer is not None:
                    self._midnight_timer.cancel()
                midnight = datetime.combine(
                    today + timedelta(days=1), time.min, tzinfo=current.tzinfo
                )
                self._midnight_timer = self.hass.loop.call_later(
                    max(0, midnight.timestamp() - current.timestamp()),
                    self.async_update_listeners,
                )
                return UsageSnapshot(local_date=today, daily=daily, hourly=hourly)
        raise UpdateFailed("local date changed during usage refresh")


def _ac_fields(usage: AcUsage) -> dict[str, Any]:
    values: dict[str, Any] = {
        "on_minutes": usage.on_minutes,
        "away_minutes": usage.away_minutes,
        "sleep_minutes": usage.sleep_minutes,
        "cool_minutes": usage.cool_minutes,
        "heat_minutes": usage.heat_minutes,
        "fan_minutes": usage.fan_minutes,
        "dry_minutes": usage.dry_minutes,
        "weather_adapt_minutes": usage.weather_adapt_minutes,
        "average_setpoint_c": usage.average_setpoint_c,
    }
    return values


def _dated_ac_row(
    history: UsageHistory,
    ac_id: int,
    index: int,
    *,
    label: Literal["day", "hour"],
) -> dict[str, Any]:
    usage = history.ac_usage(ac_id, index)
    item = history.bucket(index)
    if usage is None:
        fields: dict[str, Any] = dict.fromkeys(_MODE_FIELDS, None)
        fields["average_setpoint_c"] = None
    else:
        fields = _ac_fields(usage)
    fields["minimum_temperature_c"] = (
        None if item is None else item.minimum_temperature_c
    )
    fields["maximum_temperature_c"] = (
        None if item is None else item.maximum_temperature_c
    )
    return {label: index, **fields}


def _parse_buckets(
    body: bytes, *, resolution_minutes: int, hourly: bool
) -> tuple[UsageBucket, ...]:
    memo: dict[tuple[int, int], tuple[tuple[UsageBucket, ...], ...]] = {}
    bucket_memo: dict[int, tuple[tuple[int, UsageBucket], ...]] = {}
    budget = [_NODE_BUDGET]

    def buckets_at(offset: int) -> tuple[tuple[int, UsageBucket], ...]:
        cached = bucket_memo.get(offset)
        if cached is None:
            cached = tuple(
                _buckets_from(
                    body,
                    offset,
                    resolution_minutes=resolution_minutes,
                    hourly=hourly,
                    budget=budget,
                )
            )
            bucket_memo[offset] = cached
        return cached

    solutions = _histories(body, 0, -1, memo, buckets_at)
    if len(solutions) != 1:
        message = (
            "ambiguous usage report" if len(solutions) > 1 else "malformed usage report"
        )
        raise UsageParseError(message)
    return solutions[0]


def _histories(
    body: bytes,
    offset: int,
    previous: int,
    memo: dict[tuple[int, int], tuple[tuple[UsageBucket, ...], ...]],
    buckets_at: Callable[[int], tuple[tuple[int, UsageBucket], ...]],
) -> tuple[tuple[UsageBucket, ...], ...]:
    key = (offset, previous)
    cached = memo.get(key)
    if cached is not None:
        return cached
    if offset == len(body):
        memo[key] = ((),)
        return memo[key]
    found: list[tuple[UsageBucket, ...]] = []
    for end, bucket in buckets_at(offset):
        if bucket.index <= previous:
            continue
        for tail in _histories(body, end, bucket.index, memo, buckets_at):
            found.append((bucket, *tail))
            if len(found) > _SOLUTION_CAP:
                raise UsageParseError("ambiguous usage report")
    memo[key] = tuple(found)
    return memo[key]


def _buckets_from(
    body: bytes,
    start: int,
    *,
    resolution_minutes: int,
    hourly: bool,
    budget: list[int],
) -> list[tuple[int, UsageBucket]]:
    completion_memo: dict[
        tuple[int, bool, int, int, int, int],
        tuple[_Completion, ...],
    ] = {}

    def complete(state: _Cursor) -> tuple[_Completion, ...]:
        key = state.key()
        cached = completion_memo.get(key)
        if cached is not None:
            return cached
        budget[0] -= 1
        if budget[0] < 0:
            raise UsageParseError("ambiguous usage report")
        trailer = _trailer(
            body,
            state.offset,
            state.ac_count + state.zone_count,
            hourly=hourly,
        )
        if trailer is not None:
            index, minimum, maximum, end = trailer
            no_acs: tuple[AcUsage, ...] = ()
            no_zones: tuple[ZoneUsage, ...] = ()
            found: tuple[_Completion, ...] = (
                (end, index, minimum, maximum, no_acs, no_zones),
            )
            completion_memo[key] = found
            return found
        if state.offset >= len(body):
            completion_memo[key] = ()
            return ()
        marker = body[state.offset]
        if state.accepting_ac and _is_ac(marker):
            found = _follow_ac(
                body,
                state,
                resolution_minutes=resolution_minutes,
                complete=complete,
            )
            if len(found) > _SOLUTION_CAP:
                raise UsageParseError("ambiguous usage report")
            completion_memo[key] = found
            return found
        if _is_zone(marker):
            found = _follow_zone(
                body,
                state,
                resolution_minutes=resolution_minutes,
                complete=complete,
            )
            completion_memo[key] = found
            return found
        completion_memo[key] = ()
        return ()

    openings = complete(
        _Cursor(
            offset=start,
            accepting_ac=True,
            last_ac=-1,
            last_zone=-1,
            ac_count=0,
            zone_count=0,
        )
    )
    return [
        (
            end,
            UsageBucket(
                index=index,
                air_conditioners=air_conditioners,
                zones=zones,
                minimum_temperature_c=minimum,
                maximum_temperature_c=maximum,
            ),
        )
        for end, index, minimum, maximum, air_conditioners, zones in openings
    ]


@dataclass(frozen=True, slots=True)
class _Cursor:
    offset: int
    accepting_ac: bool
    last_ac: int
    last_zone: int
    ac_count: int
    zone_count: int

    def key(self) -> tuple[int, bool, int, int, int, int]:
        return (
            self.offset,
            self.accepting_ac,
            self.last_ac,
            self.last_zone,
            self.ac_count,
            self.zone_count,
        )


_Completion = tuple[
    int,
    int,
    float | None,
    float | None,
    tuple[AcUsage, ...],
    tuple[ZoneUsage, ...],
]
_CompleteFn = Callable[[_Cursor], tuple[_Completion, ...]]


def _follow_ac(
    body: bytes,
    state: _Cursor,
    *,
    resolution_minutes: int,
    complete: _CompleteFn,
) -> tuple[_Completion, ...]:
    if state.offset + _AC_SHORT_SIZE > len(body):
        return ()
    if body[state.offset + 1] != _AC_RECORD_KIND:
        return ()
    ac_id = body[state.offset] & _ID_MASK
    if body[state.offset] != _AC_MARKER | ac_id or ac_id <= state.last_ac:
        return ()
    average_raw = body[state.offset + 2]
    # Wire 0 can be the 4-byte form or a 12-byte form whose on time rounded down.
    on_wire = body[state.offset + 3]
    found: list[_Completion] = []
    if on_wire == 0:
        found.extend(
            _prepend_ac(
                AcUsage(
                    ac_id,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    _setpoint_c(average_raw),
                ),
                complete(
                    _Cursor(
                        offset=state.offset + _AC_SHORT_SIZE,
                        accepting_ac=True,
                        last_ac=ac_id,
                        last_zone=state.last_zone,
                        ac_count=state.ac_count + 1,
                        zone_count=state.zone_count,
                    )
                ),
            )
        )
    if state.offset + _AC_LONG_SIZE <= len(body):
        counters = body[state.offset + 3 : state.offset + _AC_LONG_SIZE]
        if counters[_DUPLICATE_ON_COUNTER] == counters[_ON_COUNTER]:
            found.extend(
                _prepend_ac(
                    _ac_from_counters(
                        ac_id,
                        average_raw,
                        counters,
                        resolution_minutes,
                    ),
                    complete(
                        _Cursor(
                            offset=state.offset + _AC_LONG_SIZE,
                            accepting_ac=True,
                            last_ac=ac_id,
                            last_zone=state.last_zone,
                            ac_count=state.ac_count + 1,
                            zone_count=state.zone_count,
                        )
                    ),
                )
            )
    return tuple(found)


def _follow_zone(
    body: bytes,
    state: _Cursor,
    *,
    resolution_minutes: int,
    complete: _CompleteFn,
) -> tuple[_Completion, ...]:
    if state.offset + _ZONE_RECORD_SIZE > len(body):
        return ()
    zone_id = body[state.offset] & _ID_MASK
    if body[state.offset] != _ZONE_MARKER | zone_id or zone_id != state.last_zone + 1:
        return ()
    zone = ZoneUsage(zone_id, body[state.offset + 1] * resolution_minutes)
    return tuple(
        (
            end,
            index,
            minimum,
            maximum,
            air_conditioners,
            (zone, *zones),
        )
        for end, index, minimum, maximum, air_conditioners, zones in complete(
            _Cursor(
                offset=state.offset + _ZONE_RECORD_SIZE,
                accepting_ac=False,
                last_ac=state.last_ac,
                last_zone=zone_id,
                ac_count=state.ac_count,
                zone_count=state.zone_count + 1,
            )
        )
    )


def _prepend_ac(usage: AcUsage, tails: tuple[_Completion, ...]) -> list[_Completion]:
    return [
        (end, index, minimum, maximum, (usage, *air_conditioners), zones)
        for end, index, minimum, maximum, air_conditioners, zones in tails
    ]


def _ac_from_counters(
    ac_id: int,
    average_raw: int,
    counters: bytes,
    resolution_minutes: int,
) -> AcUsage:
    scaled = tuple(
        value * resolution_minutes for value in counters[:_DUPLICATE_ON_COUNTER]
    )
    return AcUsage(
        ac_id,
        scaled[0],
        scaled[1],
        scaled[2],
        scaled[3],
        scaled[4],
        scaled[5],
        scaled[6],
        scaled[7],
        _setpoint_c(average_raw),
    )


def _trailer(
    body: bytes, offset: int, count: int, *, hourly: bool
) -> tuple[int, float | None, float | None, int] | None:
    if offset + _TRAILER_SIZE > len(body):
        return None
    index = body[offset]
    if body[offset + 1] != _TRAILER_KIND or body[offset + 3] != _TRAILER_FLAG:
        return None
    if body[offset + 2] != count:
        return None
    if hourly:
        if index > _HOUR_MAX:
            return None
    elif index < _DAY_MIN or index > _DAY_MAX:
        return None
    minimum = int.from_bytes(body[offset + 4 : offset + 6], "big")
    maximum = int.from_bytes(body[offset + 6 : offset + 8], "big")
    return (
        index,
        _temperature_c(minimum),
        _temperature_c(maximum),
        offset + _TRAILER_SIZE,
    )


def _setpoint_c(raw: int) -> float | None:
    if raw == 0:
        return None
    return (raw + _SETPOINT_OFFSET) / _TEMPERATURE_SCALE


def _temperature_c(raw: int) -> float | None:
    if raw >= _ABSENT_TEMPERATURE:
        return None
    return (raw - _TEMPERATURE_OFFSET) / _TEMPERATURE_SCALE


def _is_ac(marker: int) -> bool:
    return (marker & _MARKER_MASK) == _AC_MARKER


def _is_zone(marker: int) -> bool:
    return (marker & _MARKER_MASK) == _ZONE_MARKER
