"""
modules/db_management.py
"""
import os
import glob
import json
import shutil
import subprocess
from datetime import datetime
from database import postgres


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Docker: BACKUP_DIR points at a mounted volume so backups survive image updates
BACKUP_ROOT = os.environ.get("BACKUP_DIR", os.path.join(BASE_DIR, "Backups"))

CUSTOM_BACKUP_FOLDER = os.path.join(BACKUP_ROOT, "Custom")

BACKUP_LOG_PATH = os.environ.get("BACKUP_LOG_PATH", os.path.join(BASE_DIR, "backup_log.json"))

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


# Postgres backups with pg_dump
#
# Full backup  -> PLCDB2_full_<stamp>.dump  (pg_dump custom format)
#     restore:  pg_restore -d PLCDB2 --clean --if-exists --no-owner <file>
# Date range   -> PLCDB2_<from>_to_<to>_<stamp>.sql  (plain SQL)
#     restore:  psql -d PLCDB2 -f <file>
#     Every table in full, except Batches / plc_data: only the rows logged
#     in the range. pg_dump cannot filter rows, so those two tables are
#     dumped without data and their rows appended as COPY blocks.
#
# Both files are a complete database (all tables, column types, indexes,
# sequences) - not a copy into another format.
# --------------------------------------------------------

BACKUP_PREFIX = "PLCDB2_"
KEEP_BACKUPS = 10            # newest backup files kept in the Custom folder
PG_DUMP_TIMEOUT = 280        # seconds; gunicorn kills a request after 300


class BackupError(Exception):
    """Backup could not be made; the message is shown to the user."""


def _pg_tool(name):
    """Path of a PostgreSQL client program (pg_dump, ...).
    PG_BIN (env) > PATH > newest C:\\Program Files\\PostgreSQL\\<n>\\bin."""
    exe = name + (".exe" if os.name == "nt" else "")
    pg_bin = os.environ.get("PG_BIN")
    if pg_bin and os.path.isfile(os.path.join(pg_bin, exe)):
        return os.path.join(pg_bin, exe)
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        root = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "PostgreSQL")
        versions = sorted((d for d in glob.glob(os.path.join(root, "*")) if os.path.basename(d).isdigit()),
                          key=lambda d: int(os.path.basename(d)), reverse=True)
        for d in versions:
            if os.path.isfile(os.path.join(d, "bin", exe)):
                return os.path.join(d, "bin", exe)
    raise BackupError(f"{name} was not found. Install the PostgreSQL client tools "
                      f"(same version as the database) or set PG_BIN to their folder.")


def _pg_connection_args():
    cfg = postgres.DB_CONFIG
    env = dict(os.environ, PGPASSWORD=str(cfg["password"]), PGCLIENTENCODING="UTF8")
    args = ["-h", str(cfg["host"]), "-p", str(cfg["port"]), "-U", str(cfg["user"]), "-d", str(cfg["dbname"])]
    return args, env


def _run_pg_dump(extra_args, dest_path):
    args, env = _pg_connection_args()
    cmd = [_pg_tool("pg_dump"), *args, "--no-password", *extra_args, "-f", dest_path]
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=PG_DUMP_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise BackupError(f"Backup took longer than {PG_DUMP_TIMEOUT} s - try a shorter date range.")
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip().splitlines()
        # e.g. "server version: 18.0; pg_dump version: 15.4" -> client too old
        raise BackupError("pg_dump failed: " + (msg[-1] if msg else f"exit code {result.returncode}"))


def _new_backup_path(name):
    os.makedirs(CUSTOM_BACKUP_FOLDER, exist_ok=True)
    return os.path.join(CUSTOM_BACKUP_FOLDER, name)


def _finish(path, name):
    """Checks the file, records the backup and trims old ones."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise BackupError("Backup file was not created.")
    record_backup_event()
    _prune_old_backups()
    return path, name


def _prune_old_backups():
    files = sorted(glob.glob(os.path.join(CUSTOM_BACKUP_FOLDER, BACKUP_PREFIX + "*")),
                   key=os.path.getmtime, reverse=True)
    for old in files[KEEP_BACKUPS:]:
        try:
            os.remove(old)
        except OSError:
            pass


def _stamp():
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------
# Full backup
# --------------------------------------------------------

def create_full_backup():
    """Complete database in pg_dump custom format (compressed).
    Returns (path, file name)."""
    name = f"{BACKUP_PREFIX}full_{_stamp()}.dump"
    path = _new_backup_path(name)
    try:
        _run_pg_dump(["-Fc"], path)
        return _finish(path, name)
    except Exception:
        _safe_remove(path)
        raise


# --------------------------------------------------------
# Date-range backup
# --------------------------------------------------------

def create_custom_range_backup(from_date, to_date):
    """
    Complete database, but Batches / plc_data hold only the rows logged
    from from_date 00:00 to to_date 23:59:59.999 (dates 'YYYY-MM-DD').
    Plain SQL; restoring it REPLACES the database with this content.
    Returns (path, file name).
    """
    try:
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("from_date and to_date must be in YYYY-MM-DD format")
    if to_dt < from_dt:
        raise ValueError("to_date cannot be before from_date")

    from_bound = from_dt.strftime("%Y-%m-%d 00:00:00.000")
    to_bound = to_dt.strftime("%Y-%m-%d 23:59:59.999")

    name = f"{BACKUP_PREFIX}{from_dt:%Y-%m-%d}_to_{to_dt:%Y-%m-%d}_{_stamp()}.sql"
    path = _new_backup_path(name)
    try:
        exclude = []
        for table in TIME_FILTERED_TABLES:
            exclude += ["--exclude-table-data", f'public."{table}"']
        # --clean --if-exists: the file can be restored over an existing database
        _run_pg_dump(["-Fp", "--clean", "--if-exists", "--no-owner", *exclude], path)
        _append_filtered_rows(path, from_bound, to_bound)
        return _finish(path, name)
    except Exception:
        _safe_remove(path)
        raise


def _append_filtered_rows(path, from_bound, to_bound):
    """Appends 'COPY ... FROM stdin' blocks with the date-range rows of the
    time-series tables, in the format psql reads back."""
    conn = postgres.connect()
    try:
        conn.set_client_encoding("UTF8")
        with conn.cursor() as cur, open(path, "a", encoding="utf-8", newline="") as out:
            out.write("\n--\n-- Rows logged between %s and %s\n--\n\n" % (from_bound, to_bound))
            for table, ts_col in TIME_FILTERED_TABLES.items():
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position
                """, (table,))
                cols = ", ".join('"%s"' % c[0].replace('"', '""') for c in cur.fetchall())
                if not cols:
                    continue
                query = cur.mogrify(
                    f'SELECT {cols} FROM public."{table}" WHERE "{ts_col}" BETWEEN %s AND %s ORDER BY "{ts_col}"',
                    (from_bound, to_bound)).decode()
                out.write(f'COPY public."{table}" ({cols}) FROM stdin;\n')
                cur.copy_expert(f"COPY ({query}) TO STDOUT", out)
                out.write("\\.\n\n")
    finally:
        conn.close()


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