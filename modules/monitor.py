import os
import socket
import logging
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

import pandas as pd

from auth import licence
from database import postgres
from plc_connection import pylogix, snap7_plc
from modules.batch_summary import batch_summary_rows

# === Logging Setup ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.environ.get("LOG_DIR", BASE_DIR)   # Docker: a mounted volume
os.makedirs(LOG_DIR, exist_ok=True)
log_file = os.path.join(LOG_DIR, "plc_monitor.log")

root_logger = logging.getLogger()
if not any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
    handler = RotatingFileHandler(log_file, maxBytes=50 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    root_logger.addHandler(handler)

root_logger.setLevel(logging.INFO)
logging.getLogger('werkzeug').setLevel(logging.ERROR)
logging.info(f"Logging initialized -> {log_file}")
print(f"[logging] writing to: {log_file}")
# Silence noisy third-party libraries (PDF font subsetting, etc.)
for noisy in ("fontTools", "fontTools.subset", "fontTools.ttLib", "PIL", "matplotlib"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
# === PLC connection settings ===
S7_PORT = 102          # Siemens S7comm
ENIP_PORT = 44818      # Rockwell EtherNet/IP
MAX_FAILED_CYCLES = 3  # consecutive failed cycles before we reconnect
POLL_INTERVAL = 1.0    # seconds between trigger polls
RETRY_DELAY = 5.0      # seconds before re-logging a batch whose save failed
MAX_BACKOFF = 30       # max seconds between reconnect attempts
LICENCE_CHECK_INTERVAL = 600   # seconds; an expired demo stops logging within 10 min, no restart needed

MSG_NOT_READY = ("PLC is not ready to connect. "
                 "Check power, cable/Wi-Fi and the Station IP.")
MSG_NOT_CONNECTED = "PLC is not connected"

# Material names the PLC reports for unused / uninitialised silos
INVALID_MATERIALS = {'nan', 'None', '', '0.0', '-4.253529586511731e+37', '-4.253530e+37'}

# === Shared state (thread-safe) ===
stop_event = threading.Event()
data_lock = threading.Lock()
db_write_lock = threading.Lock()
start_lock = threading.Lock()

latest_data = {}
trigger_dataframes = {}
plc_thread = None
connection_state = "disconnected"   # connected | reconnecting | disconnected


def get_latest_data():
    with data_lock:
        return latest_data.copy()


def set_latest_data(data):
    global latest_data
    with data_lock:
        latest_data = data


def _set_state(state, msg=None):
    global connection_state
    connection_state = state
    if msg:
        set_latest_data({"msg": msg})


def get_status():
    return connection_state if is_running() else "disconnected"


def normalize_tags(dfPlcdb):
    """Siemens tables use data_type, Rockwell tables Data_type - use one name,
    and make the address columns proper ints."""
    df = dfPlcdb.copy()
    if "data_type" not in df.columns and "Data_type" in df.columns:
        df = df.rename(columns={"Data_type": "data_type"})
    if "data_type" in df.columns:
        df["data_type"] = df["data_type"].fillna("").astype(str).str.strip().str.upper()
    for col in ("db_number", "start_offset", "bit_offset"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    return df.reset_index(drop=True)


def life_counter_rows(dfPlcdb):
    """The two heartbeat tags (read, write): the 'Test' category rows,
    falling back to the first two rows for older tag tables."""
    rows = dfPlcdb[dfPlcdb["Category"] == "Test"]
    if len(rows) < 2:
        rows = dfPlcdb.head(2)
    return rows.reset_index(drop=True)


def df_split(dfPlcdb):
    try:
        if not dfPlcdb[dfPlcdb['Sample_mode'] == "Trigger"].empty:
            dfplcdb_Periodic = dfPlcdb[dfPlcdb["Sample_mode"] == "Periodic"]
            unique_triggers = dfPlcdb['Trigger'].dropna().unique()
            df_trigger = dfPlcdb[dfPlcdb["Name"].isin(unique_triggers)]

            with data_lock:
                trigger_dataframes.clear()
                for tag in unique_triggers:
                    trigger_dataframes[tag] = dfPlcdb[dfPlcdb['Trigger'] == tag]

            return dfplcdb_Periodic, df_trigger
        else:
            return dfPlcdb, pd.DataFrame()
    except Exception as e:
        logging.error(f"Error in df_split: {e}")
        return dfPlcdb, pd.DataFrame()


# === DB helpers ===
def load_plc_tables():
    cr = cw = er = ew = conn = None
    try:
        cr, cw, er, ew, conn = postgres.postgres()
        dfInfo = pd.read_sql_query('SELECT * FROM "Info_db";', er)
        dfPlcdb = pd.read_sql_query('SELECT * FROM "Data";', er)
        return dfInfo, normalize_tags(dfPlcdb)
    finally:
        postgres.close_postgres(cr, cw, er, ew, conn)


def info_value(dfInfo, particular, default=None):
    """Info_db value by its Particulars key (row order is not guaranteed)."""
    match = dfInfo.loc[dfInfo["Particulars"] == particular, "Info"]
    return default if match.empty else match.iloc[0]


def get_saved_node():
    dfInfo, _ = load_plc_tables()
    node = info_value(dfInfo, "Plc_IP")
    if node is None:
        node = dfInfo.loc[0, "Info"]
    return str(node).strip()


# === Connection helpers ===
def guess_driver(node):
    """'ip,rack,slot' -> Siemens (1), plain IP -> Rockwell (2)."""
    return 1 if str(node).count(',') == 2 else 2


def parse_node(server, node):
    parts = [p.strip() for p in str(node).split(',')]
    if server == 1:
        if len(parts) != 3:
            raise ValueError('Siemens format must be "ip,rack,slot" e.g. 192.168.0.1,0,1')
        return parts[0], int(parts[1]), int(parts[2])
    return parts[0], None, None


def is_plc_reachable(ip, server, timeout=2.0):
    port = S7_PORT if server == 1 else ENIP_PORT
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def close_plc(plc, server):
    if plc is None:
        return
    try:
        if server == 1:
            plc.disconnect()
        else:
            plc.Close()
    except Exception:
        pass


def _open_plc(server, node, life_rows):
    """Connect + liveness check. Returns plc object or raises."""
    plc = None
    try:
        if server == 1:
            ip, rack, slot = parse_node(1, node)
            plc = snap7_plc.snap7Connect(ip, rack, slot)
            if plc is None:
                raise ConnectionError(f"Snap7 could not connect to {ip}")
            plc.get_cpu_state()
            alive = snap7_plc.lifeCounter(plc, life_rows)
        else:
            plc = pylogix.connectABPLC(node)
            result = plc.GetPLCTime()
            if result.Status != "Success":
                raise ConnectionError(result.Status)
            alive = pylogix.lifeCounter(plc, life_rows)

        if not alive:
            raise ConnectionError("life counter check failed")
        return plc
    except Exception:
        close_plc(plc, server)
        raise


def is_running():
    return plc_thread is not None and plc_thread.is_alive() and not stop_event.is_set()


def start_monitoring(server, node=None):
    """Returns (success, message). Fails fast when the PLC is off."""
    global plc_thread, stop_event

    with start_lock:
        if is_running():
            return True, "PLC is already connected"

        lic = licence.status()
        if not lic["valid"]:
            return False, f"Licence: {lic['message']}"

        node = (node or "").strip()

        try:
            # Only touch the database first if we need the saved IP
            if not node:
                node = get_saved_node()
            ip, _, _ = parse_node(server, node)
        except ValueError as e:
            return False, str(e)
        except Exception as e:
            logging.exception("Could not read PLC configuration")
            return False, f"Could not read PLC configuration: {e}"

        # Fast network check BEFORE any slow work
        if not is_plc_reachable(ip, server, timeout=1.0):
            logging.warning(f"PLC {ip} not reachable")
            return False, MSG_NOT_READY

        try:
            _, dfPlcdb = load_plc_tables()
            plc = _open_plc(server, node, life_counter_rows(dfPlcdb))
        except Exception as e:
            logging.exception("PLC connect failed")
            return False, f"{MSG_NOT_READY} ({e})"

        _, df_trigger = df_split(dfPlcdb)
        if df_trigger.empty:
            close_plc(plc, server)
            return False, "No trigger tag configured in the PLC tag table"

        stop_event = threading.Event()
        _set_state("connected", "PLC Connected")
        plc_thread = threading.Thread(
            target=monitor_loop,
            args=(plc, server, node, dfPlcdb, df_trigger, stop_event),
            daemon=True)
        plc_thread.start()
        logging.info("PLC Connected Successfully")
        return True, "PLC Connected Successfully"


def stop_monitoring():
    stop_event.set()


def monitor_loop(plc, server, node, dfPlcdb, df_trigger, stop):
    """Runs until Disconnect is pressed. A lost PLC connection is retried with
    backoff instead of ending the thread."""
    life_rows = life_counter_rows(dfPlcdb)
    # Survive reconnects: batches already saved whose trigger reset failed
    # (must never be logged twice), and failed batches waiting for a retry.
    pending_reset = set()
    retry_at = {}
    backoff = 1

    try:
        while not stop.is_set():
            if plc is None:
                _set_state("reconnecting", "PLC connection lost - reconnecting")
                try:
                    ip, _, _ = parse_node(server, node)
                    if not is_plc_reachable(ip, server):
                        raise ConnectionError(f"{ip} not reachable")
                    plc = _open_plc(server, node, life_rows)
                    backoff = 1
                    _set_state("connected", "PLC Connected")
                    logging.info("PLC reconnected")
                except Exception as e:
                    logging.warning(f"Reconnect failed ({e}); retrying in {backoff}s")
                    stop.wait(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF)
                    continue

            _run_session(plc, server, life_rows, df_trigger, stop, pending_reset, retry_at)

            close_plc(plc, server)
            plc = None
            if not stop.is_set():
                logging.error("PLC connection lost - will reconnect")
    finally:
        close_plc(plc, server)
        _set_state("disconnected", "PLC Disconnected")
        logging.info("PLC Disconnected")


def _run_session(plc, server, life_rows, df_trigger, stop, pending_reset, retry_at):
    """Polls until stop is set or MAX_FAILED_CYCLES consecutive cycles fail."""
    failures = 0
    next_licence_check = time.monotonic() + LICENCE_CHECK_INTERVAL

    while not stop.is_set():
        if time.monotonic() >= next_licence_check:
            next_licence_check = time.monotonic() + LICENCE_CHECK_INTERVAL
            lic = licence.status(force=True)
            if not lic["valid"]:
                logging.error(f"Licence no longer valid ({lic['message']}) - PLC logging stopped")
                stop.set()
                return

        ok, active = _poll_once(plc, server, life_rows, df_trigger)

        if not ok:
            failures += 1
            if failures >= MAX_FAILED_CYCLES:
                return
            stop.wait(POLL_INTERVAL)
            continue
        failures = 0

        # Any trigger that is now low has seen our reset
        pending_reset.intersection_update(active)

        for trigger_tag in active:
            if trigger_tag in pending_reset:
                if _reset_trigger(plc, server, df_trigger, trigger_tag):
                    pending_reset.discard(trigger_tag)
                continue

            if time.monotonic() < retry_at.get(trigger_tag, 0):
                continue

            with data_lock:
                df_trigger_tag = trigger_dataframes.get(trigger_tag)

            if df_trigger_tag is None or df_trigger_tag.empty:
                logging.warning(f"Trigger DataFrame not found: {trigger_tag}")
                retry_at[trigger_tag] = time.monotonic() + RETRY_DELAY
                continue

            logging.info(f"Trigger activated: {trigger_tag}")
            if run_logging(plc, df_trigger_tag, server) is None:
                # Trigger stays set so the PLC keeps waiting; retried after RETRY_DELAY
                logging.error(f"Batch for {trigger_tag} NOT saved - trigger left set, "
                              f"retrying in {RETRY_DELAY:.0f}s")
                retry_at[trigger_tag] = time.monotonic() + RETRY_DELAY
                continue

            retry_at.pop(trigger_tag, None)
            if _reset_trigger(plc, server, df_trigger, trigger_tag):
                logging.info(f"Trigger {trigger_tag} reset - waiting for next batch")
            else:
                pending_reset.add(trigger_tag)

        stop.wait(POLL_INTERVAL)


def _poll_once(plc, server, life_rows, df_trigger):
    """One cycle: read triggers + heartbeat. Returns (healthy, active_triggers)."""
    try:
        if server == 1:
            active, df_read = snap7_plc.monitor_trigger_s7(plc, df_trigger)
            alive = snap7_plc.lifeCounter(plc, life_rows)
        elif server == 2:
            active, df_read = pylogix.monitor_trigger_ab(plc, df_trigger)
            alive = pylogix.lifeCounter(plc, life_rows)
        else:
            return False, []

        if df_read["Value"].isnull().any():
            logging.error("Trigger tag could not be read")
            return False, []
        if not alive:
            logging.error(f"PLC life counter failed : {datetime.now()}")
            return False, []
        return True, active

    except Exception:
        logging.exception("Error polling PLC")
        return False, []


def _reset_trigger(plc, server, df_trigger, trigger_tag):
    try:
        row = df_trigger[df_trigger["Name"] == trigger_tag].iloc[0]
        if server == 2:
            return pylogix.reset_trigger_tag_ab(plc, row["Tag_name"])
        return snap7_plc.reset_trigger_tag_s7(
            plc, int(row["db_number"]), int(row["start_offset"]), int(row.get("bit_offset", 0)))
    except Exception as e:
        logging.error(f"Reset Error {trigger_tag}: {e}")
        return False


AUTO_CONNECT_MAX_WAIT = 60   # seconds between auto-connect attempts (upper bound)


def _auto_connect():
    """Keeps trying until the PLC is connected (by us or by the Connect button).
    After a plant power cut the PC often boots before the PLC is reachable."""
    wait = 5
    while not is_running():
        try:
            node = get_saved_node()
            ok, msg = start_monitoring(guess_driver(node), node)
            logging.info(f"Auto-connect: {ok} - {msg}")
            if ok:
                return
        except Exception:
            logging.exception("Auto-connect failed")
        time.sleep(wait)
        wait = min(wait * 2, AUTO_CONNECT_MAX_WAIT)


def start_auto_connect():
    """Connect in the background at app start, retrying until the PLC answers."""
    threading.Thread(target=_auto_connect, daemon=True).start()


# === Batch logging ===
def _py(value):
    """numpy scalars / NaN -> plain Python values psycopg2 can adapt."""
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and pd.isna(value):
        return None
    return value


def read_batch_values(plc, df_tags, server):
    """Reads every tag of the batch. Value is None where a read failed."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

    if server == 2:  # Allen Bradley
        df = df_tags.copy().reset_index(drop=True)
        results, _ = pylogix.readABPLC_bulk(plc, df['Tag_name'].tolist())
        values = {r.TagName: r.Value for r in results if r.Status == "Success"}
        df["Value"] = [values.get(tag) for tag in df["Tag_name"]]
    elif server == 1:  # Siemens Snap7
        df = snap7_plc.read_bulk_plc_data(plc, df_tags.reset_index(drop=True))
    else:
        raise ValueError(f"Invalid Driver Selected: {server}")

    df["Timestamp"] = timestamp
    return df


def _next_batch_numbers(cur):
    """(BatchNo, DailyBatchNo). The daily counter lives in Info_db under
    'Batch_no' / 'Last_Date' and is updated in the caller's transaction."""
    cur.execute('SELECT COALESCE(MAX("BatchNo"), 0) + 1 FROM plc_data')
    batch_no = int(cur.fetchone()[0])

    cur.execute('SELECT "Particulars", "Info" FROM "Info_db" '
                'WHERE "Particulars" IN (%s, %s)', ("Last_Date", "Batch_no"))
    info = dict(cur.fetchall())

    today = datetime.now()
    today_str = today.strftime("%d-%m-%Y")

    if info.get("Last_Date") == today_str and str(info.get("Batch_no") or "").isdigit():
        daily_batch_no = int(info["Batch_no"]) + 1
    elif info.get("Last_Date") == today_str:
        # Counter row missing (older databases): continue from today's batches
        cur.execute('SELECT COUNT(DISTINCT "BatchNo") FROM plc_data WHERE "TimeStamp" >= %s',
                    (today.replace(hour=0, minute=0, second=0, microsecond=0),))
        daily_batch_no = int(cur.fetchone()[0]) + 1
    else:
        daily_batch_no = 1

    for particular, value in (("Batch_no", str(daily_batch_no)), ("Last_Date", today_str)):
        cur.execute('UPDATE "Info_db" SET "Info" = %s WHERE "Particulars" = %s',
                    (value, particular))
        if cur.rowcount == 0:
            cur.execute('INSERT INTO "Info_db" ("Id", "Particulars", "Info") '
                        'SELECT COALESCE(MAX("Id"), 0) + 1, %s, %s FROM "Info_db"',
                        (particular, value))

    return batch_no, daily_batch_no


def _insert_batch_header(cur, df):
    """One "Batches" row from the Info tags + total actual weight."""
    info_rows = df[df["Category"] == "Info"]
    info = dict(zip(info_rows["Name"], info_rows["Value"]))

    total = pd.to_numeric(df.loc[df["Name"] == "ActualWeight", "Value"], errors="coerce").sum()

    cur.execute(
        '''
        INSERT INTO "Batches"
        ("BatchNo", "TimeStamp", "Plant Name", "Recipe Name",
         "Start Date Time", "End Date Time", "Total Batch Weight")
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''',
        (
            _py(df["BatchNo"].iloc[0]),
            df["Timestamp"].iloc[0],
            _py(info.get("Plant Name")),
            _py(info.get("Recipe Name")),
            _py(info.get("Start Date Time")),
            _py(info.get("End Date Time")),
            round(float(total), 2),
        ),
    )


def _update_material_extraction(cur, df):
    """Adds this batch's ActualWeight (kg -> tons) to MaterialData.TotalExtracted.
    A material used in several silos gets the sum of all of them."""
    per_silo = (
        df[df["Name"].isin(["MaterialName", "ActualWeight"])]
        .pivot_table(index="Category", columns="Name", values="Value", aggfunc="first")
    )
    if "MaterialName" not in per_silo.columns or "ActualWeight" not in per_silo.columns:
        return

    per_silo["MaterialName"] = per_silo["MaterialName"].astype(str).str.strip()
    per_silo = per_silo[~per_silo["MaterialName"].isin(INVALID_MATERIALS)]
    per_silo["ActualWeight"] = (
        pd.to_numeric(per_silo["ActualWeight"], errors="coerce").fillna(0).div(1000).round(2)
    )
    extracted = per_silo.groupby("MaterialName")["ActualWeight"].sum()
    if extracted.empty:
        return

    cur.execute('SELECT "MaterialName", "TotalExtracted" FROM "MaterialData"')
    existing = {str(name).strip(): total for name, total in cur.fetchall()}

    for material, tons in extracted.items():
        if material not in existing:
            continue
        current = pd.to_numeric(existing[material], errors="coerce")
        current = 0.0 if pd.isna(current) else float(current)
        cur.execute(
            'UPDATE "MaterialData" SET "TotalExtracted" = %s WHERE TRIM("MaterialName") = %s',
            (str(round(current + float(tons), 5)), material),
        )


def run_logging(plc, df_tags, server):
    """Reads one batch from the PLC and saves it in ONE transaction
    (plc_data + summary + Batches + MaterialData + daily counter).
    Returns the live DataFrame, or None if nothing was saved."""
    start_time = time.monotonic()

    with db_write_lock:
        conn = None
        try:
            # ---------------- PLC Read (before opening a transaction) ----------------
            dfPlcdb = read_batch_values(plc, df_tags, server)

            missing = dfPlcdb.loc[dfPlcdb["Value"].isnull(), "Name"].unique().tolist()
            if missing:
                raise ValueError(f"No value read for {missing} - check PLC connection/tag table")

            conn = postgres.connect()
            with conn:                       # commit on success, rollback on any error
                with conn.cursor() as cur:
                    batch_no, daily_batch_no = _next_batch_numbers(cur)

                    # ---------------- Post Processing ----------------
                    dfPlcdb["BatchNo"] = batch_no
                    dfPlcdb["DailyBatchNo"] = daily_batch_no

                    category_value = dfPlcdb.loc[
                        (dfPlcdb['Name'] == "SetWeight") & (dfPlcdb['Value'] == 0.0), 'Category'
                    ]
                    if not category_value.empty:
                        dfPlcdb = dfPlcdb[~dfPlcdb['Category'].isin(category_value)]

                    dfPlcdb = postgres.calculate_silo_diff(dfPlcdb)

                    # ---------------- Insert PLC Data + Summary ----------------
                    values = [
                        tuple(_py(v) for v in row)
                        for row in dfPlcdb[['Timestamp', 'Name', 'data_type', 'Value',
                                            'Category', 'BatchNo', 'DailyBatchNo']].itertuples(index=False)
                    ]
                    values += batch_summary_rows(dfPlcdb)

                    cur.executemany(
                        '''
                        INSERT INTO "plc_data"
                        ("TimeStamp","Name","DataType","Value","Category","BatchNo","DailyBatchNo")
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ''',
                        values
                    )

                    _insert_batch_header(cur, dfPlcdb)
                    _update_material_extraction(cur, dfPlcdb)

            total_seconds = time.monotonic() - start_time
            logging.info(
                f"Batch {batch_no} (daily {daily_batch_no}) saved: "
                f"{len(values)} rows in {total_seconds:.3f} secs"
            )
            set_latest_data({"msg": f"Batch {batch_no} saved"})

            # Data for UI/API
            return dfPlcdb[['Timestamp', 'Category', 'Name', 'data_type', 'Value']].copy()

        except Exception as e:
            logging.exception(f"Error in run_logging: {e}")
            return None

        finally:
            if conn is not None:
                conn.close()
