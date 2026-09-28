import os
import socket
import logging
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler

import pandas as pd

from database import postgres
from plc_connection import pylogix, snap7_plc
from modules.batch_summary import calculate_batch_summary

# === Logging Setup ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
log_file = os.path.join(BASE_DIR, "plc_monitor.log")

root_logger = logging.getLogger()
if not any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
    handler = RotatingFileHandler(log_file, maxBytes=50 * 1024 * 1024, backupCount=3)
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
MAX_FAILED_CYCLES = 3  # consecutive failed cycles before we declare the PLC lost

MSG_NOT_READY = ("PLC is not ready to connect. "
                 "Check power, cable/Wi-Fi and the Station IP.")
MSG_NOT_CONNECTED = "PLC is not connected"

# === Shared state (thread-safe) ===
stop_event = threading.Event()
data_lock = threading.Lock()
db_write_lock = threading.Lock()
start_lock = threading.Lock()

latest_data = {}
trigger_dataframes = {}
plc_thread = None


def get_latest_data():
    with data_lock:
        return latest_data.copy()


def set_latest_data(data):
    global latest_data
    with data_lock:
        latest_data = data


def df_split(dfPlcdb):
    try:
        if not dfPlcdb[dfPlcdb['Sample_mode'] == "Trigger"].empty:
            dfplcdb_Periodic = dfPlcdb[dfPlcdb["Sample_mode"] == "Periodic"]
            unique_triggers = dfPlcdb['Trigger'].dropna().unique()
            df_trigger = dfPlcdb[dfPlcdb["Name"].isin(unique_triggers)]

            with data_lock:
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
        return dfInfo, dfPlcdb
    finally:
        postgres.close_postgres(cr, cw, er, ew, conn)


def get_saved_node():
    dfInfo, _ = load_plc_tables()
    return str(dfInfo.loc[0, "Info"]).strip()


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


def _open_plc(server, node, dfPlcdb):
    """Connect + liveness check. Returns plc object or raises."""
    plc = None
    try:
        if server == 1:
            ip, rack, slot = parse_node(1, node)
            plc = snap7_plc.snap7Connect(ip, rack, slot)
            plc.get_cpu_state()
            alive = snap7_plc.lifeCounter(plc, dfPlcdb)
        else:
            plc = pylogix.connectABPLC(node)
            result = plc.GetPLCTime()
            if result.Status != "Success":
                raise ConnectionError(result.Status)
            alive = pylogix.lifeCounter(plc, dfPlcdb)

        if not alive:
            raise ConnectionError("life counter check failed")
        return plc
    except Exception:
        if plc:
            try:
                plc.disconnect()
            except Exception:
                pass
        raise


def is_running():
    return plc_thread is not None and plc_thread.is_alive() and not stop_event.is_set()


def start_monitoring(server, node=None):
    """Returns (success, message). Fails fast when the PLC is off."""
    global plc_thread, stop_event

    with start_lock:
        if is_running():
            return True, "PLC is already connected"

        node = (node or "").strip()
        dfInfo = dfPlcdb = None

        try:
            # Only touch the database first if we need the saved IP
            if not node:
                dfInfo, dfPlcdb = load_plc_tables()
                node = str(dfInfo.loc[0, "Info"]).strip()
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
            if dfPlcdb is None:
                dfInfo, dfPlcdb = load_plc_tables()
            plc = _open_plc(server, node, dfPlcdb)
        except Exception as e:
            logging.exception("PLC connect failed")
            return False, f"{MSG_NOT_READY} ({e})"

        df_split(dfPlcdb)
        stop_event = threading.Event()         
        plc_thread = threading.Thread(
            target=monitor_loop,
            args=(plc, dfPlcdb, server, stop_event),
            daemon=True)
        plc_thread.start()
        logging.info("PLC Connected Successfully")
        return True, "PLC Connected Successfully"


def stop_monitoring():
    stop_event.set()      


def monitor_loop(plc, dfPlcdb, server, stop):
    failures = 0
    try:
        while not stop.is_set():
            ok = False
            try:
                ok = monitor_triggers(plc, dfPlcdb, server)
            except Exception:
                logging.exception("Error during monitor_triggers cycle")

            failures = 0 if ok else failures + 1
            if failures >= MAX_FAILED_CYCLES:
                logging.error("PLC connection lost - stopping monitor")
                set_latest_data({"msg": "PLC connection lost"})
                stop.set()
                break

            stop.wait(timeout=1)
    finally:
        try:
            plc.disconnect()
        except Exception:
            pass
        logging.info("PLC Disconnected")

 
def _auto_connect():
    try:
        node = get_saved_node()
        ok, msg = start_monitoring(guess_driver(node), node)
        logging.info(f"Auto-connect: {ok} - {msg}")
    except Exception:
        logging.exception("Auto-connect failed")
 
 
def start_auto_connect():
    """Try once at app start. Stays Disconnected quietly if the PLC is off."""
    threading.Thread(target=_auto_connect, daemon=True).start()


# === Trigger handling ===
def monitor_triggers(plc, dfPlcdb, server):
    current_date = datetime.now()
    Trigger_active_tags, df_trigger = [], pd.DataFrame()
    try:
        if not plc:
            return False

        if server == 2:  # Allen Bradley
            Trigger_active_tags, df_trigger = pylogix.monitor_trigger_ab(plc, pd.DataFrame())
            value = pylogix.lifeCounter(plc, dfPlcdb)
            if not value:
                logging.error(f"PLC disconnected during monitoring : {current_date}")
                return False

        elif server == 1:  # Siemens S7
            Trigger_active_tags, df_trigger = snap7_plc.monitor_trigger_s7(plc, dfPlcdb)
            value = snap7_plc.lifeCounter(plc, dfPlcdb)
            if not value:
                logging.error(f"PLC disconnected during monitoring : {current_date}")
                return False
        else:
            return False

        if Trigger_active_tags:
            logging.info(f"Info - {current_date} - Trigger activated")
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

            for trigger_tag in Trigger_active_tags:

                with data_lock:
                    df_trigger_tag = trigger_dataframes.get(trigger_tag)

                if df_trigger_tag is not None and not df_trigger_tag.empty:

                    run_logging(plc, df_trigger_tag, server)

                    # Reset the same trigger immediately
                    try:
                        trigger_row = df_trigger[df_trigger["Name"] == trigger_tag].iloc[0]

                        if server == 2:  # Allen Bradley
                            pylogix.reset_trigger_tag_ab(plc, trigger_row["Tag_name"])

                        elif server == 1:  # Siemens
                            snap7_plc.reset_trigger_tag_s7(
                                plc,
                                int(trigger_row["db_number"]),
                                int(trigger_row["start_offset"]),
                                int(trigger_row.get("bit_offset", 0))
                            )

                    except Exception as e:
                        logging.error(f"Reset Error {trigger_tag}: {e}")

                else:
                    logging.warning(f"Trigger DataFrame not found: {trigger_tag}")

            logging.info(f"Trigger - {current_date} - Reset")
            logging.info(f"Waiting - {timestamp} - for Trigger")

        return True   # healthy cycle (trigger or not)

    except Exception as e:
        logging.exception(f"Error in monitor_triggers: {e}")
        return False


def run_logging(plc, dfPlcdb, server):
    start_time = datetime.now()
    with db_write_lock:
        cursorRead = cursorWrite = engineConRead = engineConWrite = conn = None
        try:

            dfPlcdb = dfPlcdb.reset_index(drop=True)

            # ---------------- Postgres / DB Setup ----------------
            cursorRead, cursorWrite, engineConRead, engineConWrite, conn = postgres.postgres()

            dfInfo = pd.read_sql_query('SELECT * FROM "Info_db";', engineConRead)

            cursorWrite.execute('SELECT COALESCE(MAX("BatchNo"), 0) FROM plc_data')
            max_batch = cursorWrite.fetchone()[0] or 0
            new_batch_no = max_batch + 1

            # ---------------- Daily Batch Logic ----------------
            try:
                last_date = str(dfInfo.loc[7, "Info"])
                daily_batch_no = int(dfInfo.loc[8, "Info"])
            except Exception:
                last_date = ""
                daily_batch_no = 0

            current_date = datetime.now().strftime("%d-%m-%Y")

            if last_date == current_date:
                daily_batch_no += 1
            else:
                daily_batch_no = 1
                last_date = current_date

            cursorWrite.execute(
                'UPDATE "Info_db" SET "Info" = %s WHERE "Particulars" = %s',
                (daily_batch_no, "Batch_no")
            )
            cursorWrite.execute(
                'UPDATE "Info_db" SET "Info" = %s WHERE "Particulars" = %s',
                (last_date, "Last_Date")
            )
            conn.commit()

            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

            # ---------------- PLC Read ----------------
            if server == 2:  # Allen Bradley
                tags = dfPlcdb['Tag_name'].tolist()
                results, ts = pylogix.readABPLC_bulk(plc, tags)

                dfPlcdb["Value"] = None
                dfPlcdb["Timestamp"] = None

                for ret in results:
                    if ret.Status == "Success":
                        dfPlcdb.loc[dfPlcdb["Tag_name"] == ret.TagName, "Value"] = ret.Value
                        dfPlcdb.loc[dfPlcdb["Tag_name"] == ret.TagName, "Timestamp"] = ts

            elif server == 1:  # Siemens Snap7
                dfPlcdb = snap7_plc.read_bulk_plc_data(plc, dfPlcdb)
                dfPlcdb["Timestamp"] = timestamp

            else:
                raise ValueError(f"Invalid Driver Selected: {server}")

            # ---------------- Validation ----------------
            if dfPlcdb["Value"].isnull().any():
                raise ValueError("Null values found - check PLC connection")

            # ---------------- Post Processing ----------------
            dfPlcdb["BatchNo"] = new_batch_no
            dfPlcdb["DailyBatchNo"] = daily_batch_no

            category_value = dfPlcdb.loc[
                (dfPlcdb['Name'] == "SetWeight") & (dfPlcdb['Value'] == 0.0), 'Category'
            ]
            if not category_value.empty:
                dfPlcdb = dfPlcdb[~dfPlcdb['Category'].isin(category_value)]

            dfPlcdb = postgres.calculate_silo_diff(dfPlcdb)

            # ---------------- Insert PLC Data ----------------
            values = [
                (row['Timestamp'], row['Name'], row['data_type'], row['Value'], row['Category'],
                 row['BatchNo'], row['DailyBatchNo'])
                for _, row in dfPlcdb.iterrows()
            ]

            cursorWrite.executemany(
                '''
                INSERT INTO "plc_data"
                ("TimeStamp","Name","DataType","Value","Category","BatchNo","DailyBatchNo")
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''',
                values
            )

            conn.commit()

            # call summary batch
            calculate_batch_summary(dfPlcdb)

            # ---------------- Additional Processing ----------------
            numeric_types = ["REAL", "INT", "DINT", "WORD", "DWORD", "LREAL", "UINT", "UDINT"]

            mask = dfPlcdb["data_type"].str.upper().isin(numeric_types)

            dfPlcdb.loc[mask, "Value"] = pd.to_numeric(
                dfPlcdb.loc[mask, "Value"],
                errors="coerce"
            )

            postgres.insertBatch(dfPlcdb)
            postgres.insertMaterialExtraction(dfPlcdb, engineConRead, cursorWrite, conn)

            # ---------------- Logging Duration ----------------
            duration = datetime.now() - start_time
            total_seconds = round(duration.total_seconds(), 3)

            logging.info(
                f"Logging - {timestamp} - PLC data fetched in {total_seconds:.3f} secs"
            )

            # Data for UI/API
            df_live = dfPlcdb[['Timestamp', 'Category', 'Name', 'data_type', 'Value']].copy()
            return df_live

        except Exception as e:
            logging.exception(f"Error in run_logging: {e}")
            return None

        finally:
            postgres.close_postgres(cursorRead, cursorWrite, engineConRead, engineConWrite, conn)