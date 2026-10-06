"""What to pull from each device channel, and where it lands.

Field sets are BRIEF section 3, verbatim, plus the bookkeeping every load
needs: the channel's own event id (the natural key), sync_id (resumption),
record_order, and production_day_number (the reporting day key).

These specs drive the reader, the row builder and the storage target alike,
so the column names here match schema.sql exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Value kinds, applied by rows.py.
ID, TS, TEXT, NUM, DAY = "id", "ts", "text", "num", "day"

_TIMES = ("run_time", "unplanned_stop_time", "planned_stop_time", "not_scheduled_time", "ideal_time")


@dataclass(frozen=True)
class ChannelSpec:
    channel: str            # device channel name
    table: str              # destination table (schema.sql)
    key: str                # natural key field: the channel's own id
    brief_fields: tuple[str, ...]
    kinds: dict[str, str]   # field -> value kind
    rename: dict[str, str] = field(default_factory=dict)  # device field -> column
    has_end_time: bool = True
    keep_raw_day: bool = False  # store production_day_number_raw (events can span days)
    extra_fields: tuple[str, ...] = ()  # agreed additions beyond the brief's set

    @property
    def bookkeeping(self) -> tuple[str, ...]:
        return (self.key, "sync_id", "record_order", "production_day_number")

    @property
    def wanted(self) -> list[str]:
        """Every field requested, key first, no duplicates."""
        return list(dict.fromkeys((*self.bookkeeping, *self.brief_fields, *self.extra_fields)))

    def column(self, f: str) -> str:
        return self.rename.get(f, f)

    @property
    def columns(self) -> list[str]:
        cols = ["device_id", *(self.column(f) for f in self.wanted)]
        if self.keep_raw_day:
            cols.append("production_day_number_raw")
        return cols


def _kinds(*, ids=(), ts=(), text=(), num=(), day=("production_day_number",)) -> dict[str, str]:
    out: dict[str, str] = {"sync_id": ID, "record_order": ID}
    for kind, names in ((ID, ids), (TS, ts), (TEXT, text), (NUM, num), (DAY, day)):
        out.update({n: kind for n in names})
    return out


_HOURLY_FIELDS = (
    "start_time", "end_time", "shift", "shift_hour", "part", "job",
    "in_count", "good_count", "reject_count", *_TIMES, "target_count",
)
_RUN_FIELDS = (
    "start_time", "end_time", "part", "job", "duration",
    "run_time", "unplanned_stop_time", "planned_stop_time",
    "in_count", "good_count", "ideal_time", "ideal_cycle_time",
    "target_count", "target_cycles", "run_cycles", "small_stops",
)
_HOURLY_NUM = ("in_count", "good_count", "reject_count", *_TIMES, "target_count")
_RUN_NUM = ("duration", "run_time", "unplanned_stop_time", "planned_stop_time", "in_count",
            "good_count", "ideal_time", "ideal_cycle_time", "target_count", "target_cycles",
            "run_cycles", "small_stops")

SPECS: dict[str, ChannelSpec] = {
    s.channel: s
    for s in (
        ChannelSpec(
            "shift_hour", "hour_record", "shift_hour_event_id", _HOURLY_FIELDS,
            _kinds(ids=("shift_hour_event_id",), ts=("start_time", "end_time"),
                   text=("shift", "shift_hour", "part", "job"), num=_HOURLY_NUM),
        ),
        ChannelSpec(
            "shift", "shift_record", "shift_event_id", _HOURLY_FIELDS,
            _kinds(ids=("shift_event_id",), ts=("start_time", "end_time"),
                   text=("shift", "shift_hour", "part", "job"), num=_HOURLY_NUM),
            keep_raw_day=True,
        ),
        ChannelSpec(
            "process_state", "state_event", "process_state_event_id",
            ("start_time", "end_time", "duration", "process_state", "process_state_reason",
             "process_state_reason_display_name", "performance_impact", "production_phase",
             "part", "shift", "production_day_number"),
            _kinds(ids=("process_state_event_id",), ts=("start_time", "end_time"),
                   text=("process_state", "process_state_reason", "process_state_reason_display_name",
                         "performance_impact", "production_phase", "part", "shift"),
                   num=("duration",)),
            rename={"duration": "duration_s"},
            keep_raw_day=True,
        ),
        ChannelSpec(
            "part", "part_run", "part_event_id", _RUN_FIELDS,
            _kinds(ids=("part_event_id",), ts=("start_time", "end_time"),
                   text=("part", "job"), num=_RUN_NUM),
            rename={"duration": "duration_s"},
            keep_raw_day=True,
        ),
        ChannelSpec(
            "job", "job_run", "job_event_id", _RUN_FIELDS,
            _kinds(ids=("job_event_id",), ts=("start_time", "end_time"),
                   text=("part", "job"), num=_RUN_NUM),
            rename={"duration": "duration_s"},
            keep_raw_day=True,
        ),
        ChannelSpec(
            "quality_event", "quality_event", "record_id",
            ("production_day_number", "reject_reason", "reject_reason_display_name",
             "count", "record_order"),
            _kinds(ids=("record_id", "event_id", "slice_order"),
                   text=("reject_reason", "reject_reason_display_name"), num=("count",)),
            has_end_time=False,
            extra_fields=("event_id", "slice_order"),
        ),
    )
}
