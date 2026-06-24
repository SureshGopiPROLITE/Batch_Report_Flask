from http import server
import pandas as pd
import asyncio
import logging
from database import Sqlite
from modules import main
from config import sqliteCon
import json
import time
from datetime import datetime
from plc_connection import pylogix, snap7_plc
from logging.handlers import RotatingFileHandler
import sqlite3

# === Logging Setup ===
log_file = "plc_monitor.log"
handler = RotatingFileHandler(log_file, maxBytes=50*1024*1024, backupCount=3)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(handler)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# === Global Variables ===
plc_running = False
latest_data = {}  # Stores most recent PLC data for frontend polling

def get_latest_data():
    return latest_data

def set_latest_data(data):
    global latest_data
    latest_data = data

def df_split(dfPlcdb):
    try:
        if not dfPlcdb[dfPlcdb['Sample_mode'] == "Trigger"].empty:
            dfplcdb_Periodic = dfPlcdb[dfPlcdb["Sample_mode"] == "Periodic"]
            unique_triggers = dfPlcdb['Trigger'].dropna().unique()
            df_trigger = dfPlcdb[dfPlcdb["Name"].isin(unique_triggers)]
            for tag in unique_triggers:
                globals()[tag] = dfPlcdb[dfPlcdb['Trigger'] == tag]
            return dfplcdb_Periodic, df_trigger
        else:
            return dfPlcdb, pd.DataFrame()
    except Exception as e:
        logging.error(f"Error in df_split: {e}")
        return dfPlcdb, pd.DataFrame()

async def monitor_loop(plc, dfPlcdb, server):
    global plc_running
    try:
        while plc_running:
            try:
                await monitor_triggers(plc, dfPlcdb, server)
            except Exception as e:
                logging.exception(f"Error during monitor_triggers cycle: {e}")
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        logging.warning("Monitor loop cancelled.")
    finally:
        plc.Close()
        logging.info("PLC connection closed.")

def trigger_connect(server):
    global plc_running

    try:
        cursorRead, cursorWrite, engineConRead, engineConWriten, conn = Sqlite.sqlite()
        dfInfo = pd.read_sql_query('SELECT * FROM "Info_DB";',engineConRead)
        dfPlcdb = pd.read_sql_query('SELECT * FROM "Data";',engineConRead)
        node = dfInfo.loc[0, "Info"]
        df_split(dfPlcdb)

        # SNAP7
        if server == 1:
            
            plcIP, rack, slot = node.split(',')
            plc = snap7_plc.snap7Connect(plcIP,int(rack),int(slot))
            status = plc.get_cpu_state()
            print(status)
            value = snap7_plc.lifeCounter(plc, dfPlcdb)
            
           

            if status != "S7CpuStatusRun":
                print("0")
            
            if status == "S7CpuStatusRun": 
                print("TRUE")
            logging.info("PLC Connected")
            message = f"Waiting - {datetime.now()} - for Trigger"
            logging.info(message)
            logging.info("Monitoring Triggers...")
            asyncio.run(
            monitor_triggers(plc, dfPlcdb, server))
            logging.info("Connected")
            
           
            if not value:
                return "PLC Lifecounter Failed"
        
        # PYLOGIX
        elif server == 2:
            plc = pylogix.connectABPLC(node)
            result = plc.GetPLCTime()
            value = pylogix.lifeCounter(
                plc,
                dfPlcdb
            )
            print(value)


            if result.Status != "Success":
                return f"PLC Connection Failed : {result.Status}"
            if not value:
                return "PLC Lifecounter Failed"
        else:
            return "Invalid Driver"
        
        plc_running = True
        print(plc_running)
        logging.info("PLC Connected Successfully")
        # Single event loop, runs the monitor loop until plc_running is False
        asyncio.run(monitor_loop(plc, dfPlcdb, server))
        return "PLC Monitoring Stopped"
    except Exception as e:
        logging.exception("Error in trigger_connect")
        set_latest_data({"msg": f"PLC not connected : {e}"})
    return f"Error: {e}"

async def monitor_triggers(plc, dfPlcdb, server):
    current_date = datetime.now()
    try:
        
        if not plc:
            return False
       
        if server == 2: # Allen Bradley
            Trigger_active_tags, df_trigger = (pylogix.monitor_trigger_ab(plc,pd.DataFrame()))
            value = pylogix.lifeCounter(plc, dfPlcdb)
            if not value:
                logging.error(f"PLC disconnected during monitoring : {current_date}")
                return False
      
        elif server == 1: # Siemens S7
            
            Trigger_active_tags, df_trigger = (snap7_plc.monitor_trigger_s7( plc,dfPlcdb))
            print("Woring on looping")
            print(Trigger_active_tags, df_trigger)

            value = snap7_plc.lifeCounter(plc,dfPlcdb)
            if not value:
                logging.error(f"PLC disconnected during monitoring : {current_date}")
                return False
            
        
        if Trigger_active_tags:
            print("INN")

            logging.info(f"Info - {current_date} - Trigger activated")

            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

            for trigger_tag in Trigger_active_tags:

                df_trigger_tag = globals().get(trigger_tag)
                print(df_trigger_tag)

                if df_trigger_tag is not None:

                    await run_logging(
                        plc,
                        df_trigger_tag.copy(),
                        server
                    )

                    # Reset the same trigger immediately
                    try:
                        trigger_row = df_trigger[
                            df_trigger["Name"] == trigger_tag
                        ].iloc[0]

                        if server == 2:      # Allen Bradley

                            pylogix.reset_trigger_tag_ab(
                                plc,
                                trigger_row["Tag_name"]
                            )

                        elif server == 1:    # Siemens

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
    
async def run_logging(plc, dfPlcdb, server):

    start_time = datetime.now()

    try:
        

        dfPlcdb = dfPlcdb.reset_index(drop=True)

        # ---------------- SQLite / DB Setup ----------------
        cursorRead, cursorWrite, engineConRead, engineConWrite, conn = Sqlite.sqlite()

        dfInfo = pd.read_sql_query(
            'SELECT * FROM "Info_DB";',
            engineConRead
        )

        cursorWrite.execute(
            'SELECT COALESCE(MAX("BatchNo"), 0) FROM plc_data'
        )

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
            'UPDATE Info_DB SET Info = ? WHERE Particulars = ?',
            (daily_batch_no, "Batch_no")
        )

        cursorWrite.execute(
            'UPDATE Info_DB SET Info = ? WHERE Particulars = ?',
            (last_date, "Last_Date")
        )

        conn.commit()

        timestamp = datetime.now().strftime(
            '%Y-%m-%d %H:%M:%S.%f'
        )[:-3]

        # ---------------- PLC Read ----------------
        if server == 2:      # Allen Bradley

            tags = dfPlcdb['Tag_name'].tolist()

            results, ts = pylogix.readABPLC_bulk(
                plc,
                tags
            )

            dfPlcdb["Value"] = None
            dfPlcdb["Timestamp"] = None

            for ret in results:
                if ret.Status == "Success":

                    dfPlcdb.loc[
                        dfPlcdb["Tag_name"] == ret.TagName,
                        "Value"
                    ] = ret.Value

                    dfPlcdb.loc[
                        dfPlcdb["Tag_name"] == ret.TagName,
                        "Timestamp"
                    ] = ts

        elif server == 1:       # Siemens Snap7

            dfPlcdb = snap7_plc.read_bulk_plc_data(
                plc,
                dfPlcdb
            )

            dfPlcdb["Timestamp"] = timestamp

        else:
            raise ValueError(
                f"Invalid Driver Selected: {server}"
            )

        # ---------------- Validation ----------------
        if dfPlcdb["Value"].isnull().any():
            raise ValueError(
                "Null values found - check PLC connection"
            )

        # ---------------- Post Processing ----------------
        dfPlcdb["BatchNo"] = new_batch_no
        dfPlcdb["DailyBatchNo"] = daily_batch_no

        dfPlcdb = Sqlite.calculate_silo_diff(dfPlcdb)

        

        # ---------------- Insert PLC Data ----------------
        values = [
            (
                row["Timestamp"],
                row["Name"],
                row["data_type"],
                row["Value"],
                row["Category"],
                row["BatchNo"],
                row["DailyBatchNo"]
            )
            for _, row in dfPlcdb.iterrows()
        ]

        cursorWrite.executemany(
            '''
            INSERT INTO plc_data
            ("TimeStamp","Name","DataType",
             "Value","Category",
             "BatchNo","DailyBatchNo")
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            values
        )

        conn.commit()

        # # ---------------- Additional Processing ----------------
        # Sqlite.insertBatch(dfPlcdb)
        # Sqlite.insertMaterialExtraction(
        #     dfPlcdb,
        #     engineConRead,
        #     cursorWrite,
        #     conn
        # )

        # ---------------- Logging Duration ----------------
        duration = datetime.now() - start_time
        total_seconds = round(
            duration.total_seconds(),
            3
        )

        logging.info(
            f"Logging - {timestamp} - "
            f"PLC data fetched in "
            f"{total_seconds:.3f} secs"
        )

        # Data for UI/API
        df_live = dfPlcdb[
            ['Timestamp',
             'Category',
             'Name',
             'data_type',
             'Value']
        ].copy()

        return df_live

    except Exception as e:

        logging.exception(
            f"Error in run_logging: {e}"
        )

        return None

    finally:

        try:
            cursorRead.close()
        except:
            pass

        try:
            cursorWrite.close()
        except:
            pass

        try:
            conn.close()
        except:
            pass

   