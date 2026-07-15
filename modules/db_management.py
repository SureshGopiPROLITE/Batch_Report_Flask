"""
modules/db_management.py
"""
import os

import json
import shutil
import sqlite3
from datetime import datetime


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_PATH = os.path.join(BASE_DIR, "PLCDB2.db")  # confirm this matches your live DB
BACKUP_ROOT = os.path.join(BASE_DIR, "Backups")


CUSTOM_BACKUP_FOLDER = os.path.join(BACKUP_ROOT, "Custom")

BACKUP_LOG_PATH = os.path.join(BASE_DIR, "backup_log.json")

STORAGE_QUOTA_GB = 10


TIME_FILTERED_TABLES = {
    "Batches": "TimeStamp",
    "plc_data": "TimeStamp"
}

# --------------------------------------------------------
# Record / Read Last Backup
# --------------------------------------------------------

def record_backup_event():
    with open(BACKUP_LOG_PATH, "w") as f:
        json.dump({"last_backup": datetime.now().isoformat()}, f, indent=4)


def get_last_backup_info():
    if not os.path.exists(BACKUP_LOG_PATH):
        return "No Backup Yet", "Overdue"

    with open(BACKUP_LOG_PATH) as f:
        data = json.load(f)

    last_backup = datetime.fromisoformat(data["last_backup"])
    days = (datetime.now() - last_backup).days

    if days <= 60:
        status = "Healthy"
    elif days <= 180:
        status = "Warning"
    else:
        status = "Overdue"

    return last_backup.strftime("%B %d, %Y %I:%M %p"), status


# --------------------------------------------------------
# Storage Used (live DB size)
# --------------------------------------------------------

def get_storage_used_gb():
    if not os.path.exists(DB_PATH):
        return 0, STORAGE_QUOTA_GB, 0

    size = os.path.getsize(DB_PATH)
    used = round(size / (1024 ** 3), 2)
    percent = min(round((used / STORAGE_QUOTA_GB) * 100, 1), 100)

    return used, STORAGE_QUOTA_GB, percent


# --------------------------------------------------------
# Build a date-filtered copy of the database
# --------------------------------------------------------

def _build_filtered_backup(dest_path, row_filter_for_column):
    """
    Builds a new SQLite file at dest_path containing:
      - the full schema of the live database
      - for tables listed in TIME_FILTERED_TABLES: only rows matching
        the filter returned by row_filter_for_column(ts_col)
      - for all other tables: every row, copied in full

    row_filter_for_column: a function that takes the timestamp column
    name and returns (where_clause_sql, params_tuple). This lets the
    same builder serve exact-period matches (Monthly/Yearly) and
    arbitrary date-range matches (Custom) without duplicating the
    schema-copy logic.

    Examples:
      # Yearly: strftime('%Y', TimeStamp) = '2026'
      lambda ts_col: (f'strftime(\'%Y\', "{ts_col}") = ?', ("2026",))

      # Custom range: TimeStamp BETWEEN from AND to
      lambda ts_col: (f'"{ts_col}" BETWEEN ? AND ?', (from_dt, to_dt))
    """
    if os.path.exists(dest_path):
        os.remove(dest_path)

    src_conn = sqlite3.connect(DB_PATH)
    dest_conn = sqlite3.connect(dest_path)

    try:
        src_cur = src_conn.cursor()
        dest_cur = dest_conn.cursor()

        # 1. Recreate schema: tables, indexes, triggers, views
        src_cur.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        )
        schema_objects = src_cur.fetchall()

        # Create tables first, then indexes/triggers/views (order matters)
        for obj_type, name, sql in schema_objects:
            if obj_type == "table":
                dest_cur.execute(sql)

        for obj_type, name, sql in schema_objects:
            if obj_type != "table":
                dest_cur.execute(sql)

        dest_conn.commit()

        # 2. Copy data table by table
        table_names = [name for obj_type, name, sql in schema_objects if obj_type == "table"]

        for table in table_names:
            src_cur.execute(f'PRAGMA table_info("{table}")')
            columns = [row[1] for row in src_cur.fetchall()]
            col_list = ", ".join(f'"{c}"' for c in columns)
            placeholders = ", ".join("?" for _ in columns)

            if table in TIME_FILTERED_TABLES:
                ts_col = TIME_FILTERED_TABLES[table]
                where_clause, params = row_filter_for_column(ts_col)
                src_cur.execute(
                    f'SELECT {col_list} FROM "{table}" WHERE {where_clause}',
                    params
                )
            else:
                src_cur.execute(f'SELECT {col_list} FROM "{table}"')

            rows = src_cur.fetchall()
            if rows:
                dest_cur.executemany(
                    f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})',
                    rows
                )

        dest_conn.commit()

    finally:
        src_conn.close()
        dest_conn.close()




# --------------------------------------------------------
# Create a Custom Date-Range Backup (standalone -- not part
# of the Backup Type / Backup File dropdown filter)
# --------------------------------------------------------

def create_custom_range_backup(from_date, to_date):
    """
    Builds a backup containing only rows between from_date and to_date
    (inclusive) for time-series tables (Batches, plc_data). Other tables
    (recipes, users, MaterialData, etc.) are copied in full, same as
    Monthly/Yearly, so the resulting file is still a complete, standalone
    database.

    from_date, to_date: strings in 'YYYY-MM-DD' format (e.g. from an
    HTML <input type="date">). The upper bound is automatically
    extended to the end of that day (23:59:59.999), so picking the
    same day for both from_date and to_date captures that entire day.

    Each call creates a NEW file (does not overwrite), since custom
    ranges are one-off exports rather than a recurring per-period slot.

    Returns (custom_path, custom_name).
    """
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError("Database file not found")

    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("from_date and to_date must be in YYYY-MM-DD format")

    if to_dt < from_dt:
        raise ValueError("to_date cannot be before from_date")

    # Extend the "to" bound to the very end of that day, so the whole
    # final day's records are included (not cut off at midnight).
    from_bound = from_dt.strftime("%Y-%m-%d 00:00:00.000")
    to_bound = to_dt.strftime("%Y-%m-%d 23:59:59.999")

    now = datetime.now()
    custom_name = (
        f"Backup_PLCDB2{from_dt.strftime('%Y-%m-%d')}"
        f"_to_{to_dt.strftime('%Y-%m-%d')}"
        f"_{datetime.now().strftime('%H-%M-%S')}.db"
    )
    custom_path = os.path.join(CUSTOM_BACKUP_FOLDER, custom_name)

    _build_filtered_backup(
        custom_path,
        lambda ts_col: (f'"{ts_col}" BETWEEN ? AND ?', (from_bound, to_bound))
    )
    record_backup_event()   
    return custom_path, custom_name




# --------------------------------------------------------
# Create a Full (unfiltered) backup via the Custom route,
# used when from_date/to_date are both left blank.
# --------------------------------------------------------

def create_full_backup():
    
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError("Database file not found")

    now = datetime.now()
    full_name = f"Full_Backup_PLCDB2.db"
    full_path = os.path.join(CUSTOM_BACKUP_FOLDER, full_name)

    shutil.copy2(DB_PATH, full_path)
    record_backup_event()   
    return full_path, full_name




# --------------------------------------------------------
# Get Database Management Data (for the settings page)
# --------------------------------------------------------

def get_database_management_data():
    last_backup, status = get_last_backup_info()
    used, quota, percent = get_storage_used_gb()

    return {
        "last_backup": last_backup,
        "status": status,
        "storage_used_gb": used,
        "storage_quota_gb": quota,
        "storage_percent": percent,
    }