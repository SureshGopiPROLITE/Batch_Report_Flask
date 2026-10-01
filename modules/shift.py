"""Shift of a batch, from its start time.

Shifts are configured in Settings as start times (Info_db "Shift_Times"),
e.g. "A=06:00,B=14:00,C=22:00": a batch starting at 23:30 or 03:00 is in
shift C, 06:00-13:59 is A. A "Shift" tag sent by the PLC (Info category)
takes precedence when present.
"""
import re
from functools import lru_cache
from datetime import datetime

import pandas as pd

INFO_KEY = "Shift_Times"
DEFAULT_SHIFTS = "A=06:00,B=14:00,C=22:00"
_ITEM = re.compile(r"^\s*([^=,]+?)\s*=\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")


def parse(text):
    """'A=06:00,B=14:00' -> [('A', 360), ('B', 840)] sorted by start. ValueError if invalid."""
    items = []
    for part in str(text or "").split(","):
        if not part.strip():
            continue
        m = _ITEM.match(part)
        if not m:
            raise ValueError(f"'{part.strip()}' - use NAME=HH:MM, e.g. A=06:00")
        items.append((m.group(1), int(m.group(2)) * 60 + int(m.group(3))))
    if not items:
        raise ValueError("Enter at least one shift, e.g. A=06:00,B=14:00,C=22:00")
    names = [n for n, _ in items]
    starts = [m for _, m in items]
    if len(set(names)) != len(names) or len(set(starts)) != len(starts):
        raise ValueError("Each shift needs its own name and start time")
    return sorted(items, key=lambda x: x[1])


def load(cur):
    """Configured shifts from Info_db (default A/B/C, 8 hours each)."""
    cur.execute('SELECT "Info" FROM "Info_db" WHERE "Particulars" = %s', (INFO_KEY,))
    row = cur.fetchone()
    try:
        return parse(row[0] if row and row[0] else DEFAULT_SHIFTS)
    except ValueError:
        return parse(DEFAULT_SHIFTS)


def to_text(shifts):
    return ",".join(f"{n}={m // 60:02d}:{m % 60:02d}" for n, m in shifts)


# Formats the PLC / logger write; tried first because pandas' guessing parser
# costs ~2.5 ms per value (35 s for a two-month report).
_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M",
            "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M")


@lru_cache(maxsize=4096)
def _parse_text(text):
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    ts = pd.to_datetime(text, errors="coerce", dayfirst=not re.match(r"^\d{4}-", text))
    return None if pd.isna(ts) else ts.to_pydatetime().replace(tzinfo=None)


def parse_time(value):
    """PLC start times come as '2026-07-31 15:48:01' or '11-2-2025 11:14' (day first)."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() in ("", "N/A", "None", "nan"):
        return None
    return _parse_text(str(value).strip())


def name_for(when, shifts):
    """Shift name for a datetime: the last shift started at or before that time;
    before the first start of the day it is still the previous night's shift."""
    if when is None:
        return ""
    minute = when.hour * 60 + when.minute
    current = shifts[-1][0]
    for name, start in shifts:
        if minute >= start:
            current = name
    return current


def for_batch(info, shifts, fallback=None):
    """info: dict of the batch's Info tags. PLC 'Shift' tag wins, then the
    start time, then fallback (e.g. the logged time)."""
    plc_shift = str(info.get("Shift") or "").strip()
    if plc_shift and plc_shift not in ("N/A", "None", "nan"):
        return plc_shift
    when = parse_time(info.get("Start Date Time"))
    if when is None and fallback is not None:
        when = parse_time(fallback)
    return name_for(when, shifts)
