from http import server
import pandas as pd
import asyncio
import logging
from database import postgres
from modules import main
from config import sqliteCon
from plc_connection import pylogix, snap7_plc
import os
import logging
from datetime import datetime
import threading
from logging.handlers import RotatingFileHandler
# Generate Summary
from modules.batch_summary import calculate_batch_summary

# === Logging Setup ===

# Use an ABSOLUTE path based on this file's location, not the process's
# current working directory. If your app is started from a different
# folder (systemd, a service wrapper, a different shell, Flask's
# debug reloader subprocess, etc.), a bare "plc_monitor.log" can end up
# being created somewhere you're not looking — this fixes that.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
log_file = os.path.join(BASE_DIR, "plc_monitor.log")

root_logger = logging.getLogger()

# Guard against adding the handler twice. If this module gets imported
# more than once (Flask's debug=True reloader runs your script in a
# child process and can re-trigger this), you'd otherwise end up with
# 2+ RotatingFileHandlers writing the same line twice, or — depending
# on setup order — handlers pointing at stale state.
if not any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
    handler = RotatingFileHandler(log_file, maxBytes=50 * 1024 * 1024, backupCount=3)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

root_logger.setLevel(logging.INFO)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# Sanity check on startup — confirms the handler can actually write,
# and tells you exactly where the file is on disk.
logging.info(f"Logging initialized -> {log_file}")
print(f"[logging] writing to: {log_file}")


# === Global Variables ===
plc_running = False


latest_data = {}  # Stores most recent PLC data for frontend polling


# === Shared State (thread-safe) ===
stop_event = threading.Event() #Used to stop monitoring safely.

data_lock = threading.Lock() #Prevents multiple threads from modifying data simultaneously.
db_write_lock = threading.Lock()

latest_data = {}
trigger_dataframes = {}
plc_thread = None  # set by start_monitoring(); inspect with is_running()


# === Helpers for shared state ===
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

            for tag in unique_triggers:
                with data_lock:
                    trigger_dataframes[tag] = dfPlcdb[dfPlcdb['Trigger'] == tag]

            return dfplcdb_Periodic, df_trigger
        else:
            return dfPlcdb, pd.DataFrame()
    except Exception as e:
        logging.error(f"Error in df_split: {e}")
        return dfPlcdb, pd.DataFrame()

# === Entry points to call from your Flask routes ===
def is_running():
    """True if the monitoring thread is alive and hasn't been told to stop."""
    return plc_thread is not None and plc_thread.is_alive() and not stop_event.is_set()


def start_monitoring(server):
    """Call this from your Flask route to start monitoring. Returns the thread object."""
    global plc_thread
    stop_event.clear()
    plc_thread = threading.Thread(target=trigger_connect, args=(server,), daemon=True)
    plc_thread.start()
    return plc_thread


def stop_monitoring():
    """Call this from your Flask route to stop monitoring."""
    stop_event.set()


# === Monitoring loop (runs inside a background thread) ===
def monitor_loop(plc, dfPlcdb, server):
    try:
        while not stop_event.is_set():
            try:
                monitor_triggers(plc, dfPlcdb, server)
            except Exception as e:
                logging.exception(f"Error during monitor_triggers cycle: {e}")

            # Sleeps up to 5sec, but wakes immediately if stop_event is set
            stop_event.wait(timeout=1)
    finally:
        if plc:
         plc.disconnect()
        logging.info("PLC Disconnected Successfully")


def trigger_connect(server):
    # FIX: this function used to open a postgres() connection set here
    # and hang onto it for the rest of the function (and effectively for
    # the entire monitoring session, since monitor_loop below blocks for
    # as long as monitoring runs). engineConRead/engineConWrite/conn/
    # cursorRead/cursorWrite are only actually needed for the two reads
    # right below - close them immediately after, inside try/finally, so
    # a restart of monitoring (stop_monitoring -> start_monitoring) can't
    # accumulate leaked connections session after session.
    cursorRead = cursorWrite = engineConRead = engineConWriten = conn = None
    try:
        cursorRead, cursorWrite, engineConRead, engineConWriten, conn = postgres.postgres()
        dfInfo = pd.read_sql_query('SELECT * FROM "Info_db";', engineConRead)
        dfPlcdb = pd.read_sql_query('SELECT * FROM "Data";', engineConRead)
    finally:
        postgres.close_postgres(cursorRead, cursorWrite, engineConRead, engineConWriten, conn)

    try:
        node = dfInfo.loc[0, "Info"]
        df_split(dfPlcdb)

        # SNAP7
        if server == 1:
            plcIP, rack, slot = node.split(',')
            plc = snap7_plc.snap7Connect(plcIP, int(rack), int(slot))
            status = plc.get_cpu_state()
            
            print(status)
            value = snap7_plc.lifeCounter(plc, dfPlcdb)
            if not value:
                try:
                    plc.disconnect()
                except:
                    pass

                return {
                    "success": False,
                    "message": "PLC is unreachable."
    }

            if status != "S7CpuStatusRun":
                print("0")
            if status == "S7CpuStatusRun":
                print("TRUE")

            logging.info("PLC Connected")
            message = f"Waiting - {datetime.now()} - for Trigger"
            logging.info(message)
            logging.info("Monitoring Triggers...")

            # One warm-up cycle before entering the main loop
            monitor_triggers(plc, dfPlcdb, server)
            

            if not value:
                return "PLC Lifecounter Failed"

        # PYLOGIX
        elif server == 2:
            plc = pylogix.connectABPLC(node)
            result = plc.GetPLCTime()
            value = pylogix.lifeCounter(plc, dfPlcdb)
            print(value)

            if result.Status != "Success":
                return f"PLC Connection Failed : {result.Status}"
            if not value:
                return "PLC Lifecounter Failed"
        else:
            return "Invalid Driver"

        logging.info("PLC Connected Successfully")

        # Blocks this thread for the lifetime of the monitoring session
        monitor_loop(plc, dfPlcdb, server)
        return "PLC Monitoring Stopped"



    except Exception as e:
        logging.exception("Error in trigger_connect")
        set_latest_data({"msg": f"PLC not connected : {e}"})
        return f"Error: {e}"


def monitor_triggers(plc, dfPlcdb, server):
    current_date = datetime.now()
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
            print("Woring on looping")
            print(Trigger_active_tags, df_trigger)

            value = snap7_plc.lifeCounter(plc, dfPlcdb)
            if not value:
                logging.error(f"PLC disconnected during monitoring : {current_date}")
                return False

        if Trigger_active_tags:
           
            logging.info(f"Info - {current_date} - Trigger activated")
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

            for trigger_tag in Trigger_active_tags:

                with data_lock:
                    df_trigger_tag = trigger_dataframes.get(trigger_tag)

                print(df_trigger_tag)

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
                        print(f"Reset Error {trigger_tag}: {e}")

                else:
                    print(f"Trigger DataFrame not found: {trigger_tag}")

            logging.info(f"Trigger - {current_date} - Reset")
            logging.info(f"Waiting - {timestamp} - for Trigger")

            return True

    except Exception as e:
        print(f"Error - {current_date} - {e}")
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
            print(dfInfo)

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
            print(dfPlcdb)
            
            
            
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

             #  call summary batch
            calculate_batch_summary(dfPlcdb)
        
            
            
            # ---------------- Additional Processing ----------------

            # Convert numeric values to numeric dtype
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