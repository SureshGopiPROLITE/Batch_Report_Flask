"""
setup_timescaledb.py

Run this AFTER your SQLite -> Postgres migration script has finished
copying tables into PLCDB2, and AFTER you have swapped the postgres
Docker image for timescale/timescaledb (same data volume, same major
version). This script:

  1. Enables the TimescaleDB extension.
  2. Inspects each table for a likely timestamp column.
  3. Casts that column to timestamptz if it isn't already (using the
     SOURCE_TIMEZONE below, not blindly assuming UTC).
  4. Converts the table into a hypertable in place (existing rows are
     migrated into chunks - you do NOT need to re-import from SQLite).
  5. Adds a supporting index on (segment_column, time) if configured.
  6. Optionally adds compression (with segmentby) + retention policies.

Edit the CONFIG section below before running.
"""

import psycopg2
from psycopg2 import sql

# ------------------- CONFIG -------------------

PG_CONFIG = dict(
    host="localhost",
    port=5434,
    dbname="PLCDB2",
    user="postgres",
    password="12345678",
)

# Timezone your source timestamps were actually recorded in.
# If your PLC/sensor data is naive (no tz info) and was logged in IST,
# this MUST be 'Asia/Kolkata', not 'UTC' - otherwise every converted
# timestamp will be silently off by 5.5 hours.
SOURCE_TIMEZONE = "Asia/Kolkata"  # confirmed: your compose file sets TZ: Asia/Kolkata

# Tables to convert -> the name of their timestamp column.
# Leave a table out of this dict and the script will try to
# auto-detect a timestamp column by common naming patterns.
TABLE_TIME_COLUMNS = {
    "plc_data": "TimeStamp",
    "Batches": "TimeStamp",
}

# Optional: tables -> column used to segment data (device/tag/PLC id etc).
# Used for compress_segmentby and for the supporting index. Leave a table
# out if it has no natural segment column.
TABLE_SEGMENT_COLUMNS = {
    "plc_data": "Name",         # tag name - plc_data has many tags interleaved
    "Batches": "Plant Name",    # has a space in it - sql.Identifier() handles this fine
}

# Tables to skip entirely (lookup/config tables, not time-series)
SKIP_TABLES = {
    "Data",            # tag/register definitions, not readings - no timestamp
    "Info_db",         # lookup table
    "MaterialData",    # per-batch snapshot, no timestamp column
    "RecipeTagName",   # tag definitions
    "recipeData",      # recipe definitions
    "recipes",         # lookup table
    "users",           # app users table
}

# Common column name patterns used for auto-detection
CANDIDATE_TIME_COLUMN_NAMES = [
    "timestamp", "time", "ts", "datetime", "date",
    "created_at", "recorded_at", "event_time", "log_time",
]

# Chunk interval for create_hypertable. Default TimescaleDB behavior is
# 7 days if unset, which is often wrong for high-frequency PLC data.
# Aim for chunks that comfortably fit ~25% of available RAM.
CHUNK_TIME_INTERVAL = "1 day"

# Enable compression policy on hypertables (compress chunks older than this)
ENABLE_COMPRESSION = True
COMPRESS_AFTER = "7 days"

# Enable retention policy (drop chunks older than this). Set to None to skip.
RETENTION_AFTER = None  # e.g. "180 days"

# ------------------------------------------------


def get_all_tables(cur):
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE';
    """)
    return [r[0] for r in cur.fetchall()]


def get_columns(cur, table):
    cur.execute("""
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s;
    """, (table,))
    return cur.fetchall()


def guess_time_column(columns):
    col_names = {c[0].lower(): c[0] for c in columns}
    for candidate in CANDIDATE_TIME_COLUMN_NAMES:
        if candidate in col_names:
            return col_names[candidate]
    return None


def is_hypertable(cur, table):
    cur.execute("""
        SELECT 1 FROM timescaledb_information.hypertables
        WHERE hypertable_name = %s;
    """, (table,))
    return cur.fetchone() is not None


def ensure_timestamptz(cur, table, column, current_type):
    if current_type == "timestamp with time zone":
        return  # already correct

    print(f"    Casting {table}.{column} ({current_type}) -> timestamptz "
          f"(source tz: {SOURCE_TIMEZONE})")

    if current_type in ("timestamp without time zone",):
        cast_expr = sql.SQL("{} AT TIME ZONE {}").format(
            sql.Identifier(column), sql.Literal(SOURCE_TIMEZONE)
        )
    elif current_type in ("text", "character varying", "character"):
        # Assumes ISO-8601-ish text; adjust the format if your data differs
        cast_expr = sql.SQL("{}::timestamptz").format(sql.Identifier(column))
    elif current_type in ("double precision", "real", "bigint", "integer", "numeric"):
        # Assumes Unix epoch seconds; use / 1000.0 if your data is milliseconds
        cast_expr = sql.SQL("to_timestamp({})").format(sql.Identifier(column))
    else:
        raise ValueError(f"Don't know how to cast type {current_type} for {table}.{column}")

    alter_stmt = sql.SQL("ALTER TABLE {table} ALTER COLUMN {col} TYPE timestamptz USING {expr};").format(
        table=sql.Identifier(table),
        col=sql.Identifier(column),
        expr=cast_expr,
    )
    cur.execute(alter_stmt)


def make_hypertable(cur, table, column):
    print(f"    Creating hypertable on {table}({column}), "
          f"chunk_time_interval = {CHUNK_TIME_INTERVAL}")

    quoted_table = f'"{table}"'
    cur.execute(
        "SELECT create_hypertable(%s, %s, if_not_exists => TRUE, "
        "migrate_data => TRUE, chunk_time_interval => INTERVAL %s);",
        (quoted_table, column, CHUNK_TIME_INTERVAL),
    )


def make_supporting_index(cur, table, time_column, segment_column):
    if not segment_column:
        return
    index_name = f"idx_{table}_{segment_column}_{time_column}"
    print(f"    Creating supporting index {index_name} on "
          f"({segment_column}, {time_column} DESC)")
    cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {idx} ON {table} ({seg}, {col} DESC);").format(
        idx=sql.Identifier(index_name),
        table=sql.Identifier(table),
        seg=sql.Identifier(segment_column),
        col=sql.Identifier(time_column),
    ))


def apply_compression(cur, table, time_column, segment_column):
    print(f"    Enabling compression on {table}, compress after {COMPRESS_AFTER}"
          + (f", segmentby={segment_column}" if segment_column else ""))

    # compress_orderby/compress_segmentby are stored as text and re-parsed
    # later as identifiers, which applies normal case-folding unless the
    # embedded value is itself double-quoted - so wrap mixed-case column
    # names (e.g. "TimeStamp") here or they'll be looked up as lowercase.
    opts = [sql.SQL("timescaledb.compress")]
    opts.append(sql.SQL("timescaledb.compress_orderby = {}").format(sql.Literal(f'"{time_column}"')))
    if segment_column:
        opts.append(sql.SQL("timescaledb.compress_segmentby = {}").format(sql.Literal(f'"{segment_column}"')))

    cur.execute(sql.SQL("ALTER TABLE {table} SET ({opts});").format(
        table=sql.Identifier(table),
        opts=sql.SQL(", ").join(opts),
    ))
    cur.execute("SELECT add_compression_policy(%s, INTERVAL %s);", (f'"{table}"', COMPRESS_AFTER))


def apply_retention(cur, table):
    print(f"    Adding retention policy on {table}, drop after {RETENTION_AFTER}")
    cur.execute("SELECT add_retention_policy(%s, INTERVAL %s);", (f'"{table}"', RETENTION_AFTER))


def main():
    conn = psycopg2.connect(**PG_CONFIG)
    conn.autocommit = True
    cur = conn.cursor()

    print("Enabling TimescaleDB extension...")
    cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")

    tables = get_all_tables(cur)
    print(f"Found {len(tables)} tables.")

    for table in tables:
        if table in SKIP_TABLES:
            print(f"Skipping {table} (in SKIP_TABLES)")
            continue

        if is_hypertable(cur, table):
            print(f"Skipping {table}: already a hypertable.")
            continue

        columns = get_columns(cur, table)
        time_col = TABLE_TIME_COLUMNS.get(table) or guess_time_column(columns)

        if not time_col:
            print(f"Skipping {table}: no timestamp column found or configured.")
            continue

        col_type = dict((c[0], c[1]) for c in columns)[time_col]
        segment_col = TABLE_SEGMENT_COLUMNS.get(table)

        print(f"Processing {table} (time column: {time_col}, type: {col_type})")
        try:
            ensure_timestamptz(cur, table, time_col, col_type)
            make_hypertable(cur, table, time_col)
            make_supporting_index(cur, table, time_col, segment_col)

            if ENABLE_COMPRESSION:
                apply_compression(cur, table, time_col, segment_col)
            if RETENTION_AFTER:
                apply_retention(cur, table)

        except Exception as e:
            print(f"  FAILED on {table}: {e}")

    cur.close()
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()