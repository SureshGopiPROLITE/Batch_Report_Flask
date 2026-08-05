"""
modules/db_management.py
"""
import os
import json
import shutil
import sqlite3
import psycopg2
from datetime import datetime
from database import postgres


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BACKUP_ROOT = os.path.join(BASE_DIR, "Backups")

CUSTOM_BACKUP_FOLDER = os.path.join(BACKUP_ROOT, "Custom")

BACKUP_LOG_PATH = os.path.join(BASE_DIR, "backup_log.json")

STORAGE_QUOTA_GB = 10


TIME_FILTERED_TABLES = {
    "Batches": "TimeStamp",
    "plc_data": "TimeStamp"
}


SQLITE_RESERVED_TABLES = {
    "sqlite_sequence",
    "sqlite_master",
    "sqlite_stat1",
    "sqlite_stat4",
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
 
    try:
        cursorRead, cursorWrite, engineConRead, engineConWrite, conn = postgres.postgres()
        try:
            cursorRead.execute("SELECT pg_database_size(current_database())")
            size = cursorRead.fetchone()[0]
        finally:
            for c in (cursorRead, cursorWrite, conn):
                try:
                    c.close()
                except Exception:
                    pass
    except Exception:
        return 0, STORAGE_QUOTA_GB, 0

    used = round(size / (1024 ** 3), 2)
    percent = min(round((used / STORAGE_QUOTA_GB) * 100, 1), 100)

    return used, STORAGE_QUOTA_GB, percent


# --------------------------------------------------------
# Postgres schema introspection helpers
# --------------------------------------------------------


_PG_TO_SQLITE_TYPE = {
    "integer": "INTEGER",
    "bigint": "INTEGER",
    "smallint": "INTEGER",
    "boolean": "INTEGER",
    "real": "REAL",
    "double precision": "REAL",
    "numeric": "REAL",
}


def _sqlite_type_for(pg_type):
    return _PG_TO_SQLITE_TYPE.get(pg_type, "TEXT")


def _get_postgres_tables(cur):
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """)
    tables = [row[0] for row in cur.fetchall()]

   
    return [t for t in tables if t.lower() not in SQLITE_RESERVED_TABLES]


def _get_postgres_columns(cur, table):
    """Returns [(column_name, data_type), ...] in column order."""
    cur.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """, (table,))
    return cur.fetchall()


# --------------------------------------------------------
# Build a date-filtered copy of the database
# --------------------------------------------------------

def _build_filtered_backup(dest_path, row_filter_for_column):
   
    if os.path.exists(dest_path):
        os.remove(dest_path)

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    try:
        cursorRead, cursorWrite, engineConRead, engineConWrite, src_conn = postgres.postgres()
    except Exception as e:

        raise FileNotFoundError(f"Could not connect to the database: {e}")

    dest_conn = sqlite3.connect(dest_path)

    try:
        src_cur = cursorRead
        dest_cur = dest_conn.cursor()

        table_names = _get_postgres_tables(src_cur)

        # 1. Recreate schema in the SQLite destination
        table_columns = {}
        for table in table_names:
            columns = _get_postgres_columns(src_cur, table)
            table_columns[table] = columns
            col_defs = ", ".join(f'"{col}" {_sqlite_type_for(dtype)}' for col, dtype in columns)
            dest_cur.execute(f'CREATE TABLE "{table}" ({col_defs})')

        dest_conn.commit()

        # 2. Copy data table by table
        for table in table_names:
            columns = [col for col, _ in table_columns[table]]
            col_list = ", ".join(f'"{c}"' for c in columns)
            # Destination is SQLite -> "?" placeholders, NOT Postgres's "%s"
            placeholders_sqlite = ", ".join("?" for _ in columns)

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
                    f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders_sqlite})',
                    rows
                )

        dest_conn.commit()

    finally:
        for c in (cursorRead, cursorWrite, src_conn):
            try:
                c.close()
            except Exception:
                pass
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
        lambda ts_col: (f'"{ts_col}" BETWEEN %s AND %s', (from_bound, to_bound))
    )
    record_backup_event()
    return custom_path, custom_name




# --------------------------------------------------------
# Create a Full (unfiltered) backup via the Custom route,
# used when from_date/to_date are both left blank.
# --------------------------------------------------------

def create_full_backup():
    """
    Was: shutil.copy2(DB_PATH, full_path) — a raw file copy, which only
    works when the source database IS a file. Now rebuilds a fresh
    SQLite snapshot straight from the live Postgres data, reusing the
    same schema-copy machinery as create_custom_range_backup() with an
    always-true filter so every row in every table is included.
    """
    now = datetime.now()
    full_name = "Full_Backup_PLCDB2.db"
    full_path = os.path.join(CUSTOM_BACKUP_FOLDER, full_name)

    _build_filtered_backup(
        full_path,
        lambda ts_col: ("TRUE", ())
    )
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