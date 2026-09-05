import pandas as pd
import asyncio
import logging
from modules import main
from config import sqliteCon
import json
import time
from datetime import datetime
from plc_connection import pylogix
from logging.handlers import RotatingFileHandler

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

async def monitor_loop(plc, dfPlcdb):
    global plc_running
    try:
        while plc_running:
            await monitor_triggers(plc, dfPlcdb)
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        logging.warning("Monitor loop cancelled.")
    finally:
        plc.Close()
        logging.info("PLC connection closed.")

def trigger_connect():
    global plc_running
    try:
        print("Connecting to PLC...")
        engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()
        dfInfo = pd.read_sql_query('SELECT * FROM "Info_DB";', engineConRead)
        dfPlcdb = pd.read_sql_query('SELECT * FROM "Data";', engineConRead)

        plcIP = dfInfo.loc[0, 'Info']
        plc = pylogix.connectABPLC(plcIP)
        status = plc.GetPLCTime()

        if status.Status == 'Success':  
            logging.info("PLC is connected.")
            plc_running = True
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            task = loop.create_task(monitor_loop(plc, dfPlcdb))

            while plc_running:
                loop.run_until_complete(asyncio.sleep(1))

            task.cancel()
            loop.run_until_complete(task)
            loop.close()
            return "Monitoring stopped"
        else:
            logging.error(f"PLC connection failed: {status.Status}")
            return f"PLC connection failed: {status.Status}"

    except Exception as e:
        logging.exception(" Error in trigger_connect:")
        set_latest_data({"msg": f"PLC is not connected: {e}"})
        return f"Error: {e}"

async def monitor_triggers(plc, dfPlcdb):
    try:
        print("Monitoring...")
        if not plc:
            return False

        dfplcdb_Periodic, df_trigger = df_split(dfPlcdb)

        trigger_tags, df_trigger = pylogix.monitor_trigger_ab(plc, df_trigger)

        if not pylogix.lifeCounter(plc, dfPlcdb):
            logging.warning(" PLC disconnected during monitoring.")
            return False

        if trigger_tags:
            logging.info(f"Trigger tags active: {trigger_tags}")
            for tag in trigger_tags:
                df_active = globals().get(tag)
                if df_active is not None:
                    run_logging(plc, df_active)

            df_trigger[df_trigger['Value'] == True].apply(
                lambda row: pylogix.reset_trigger_tag_ab(plc, row['Tag_name']),
                axis=1
            )
            logging.info("Trigger tags reset")

        return True

    except Exception as e:
        logging.exception(" Error in monitor_triggers:")
        return False

def run_logging(plc, df_Trigger_active_tag):
    try:
        conn, cursor = sqliteCon.get_db_connection()

        df_Trigger_active_tag = df_Trigger_active_tag.reset_index(drop=True)

        dfInfo = pd.read_sql("SELECT * FROM info_db", conn)
        cursor.execute("SELECT COALESCE(MAX(\"BatchNo\"), 0) FROM plc_data")
        max_batch = cursor.fetchone()[0]
        new_batch = max_batch + 1

        today = datetime.now().strftime('%d-%m-%Y')
        last_date = dfInfo.loc[dfInfo['Particulars'] == 'Last_Date', 'Info'].values[0]
        daily_batch = int(dfInfo.loc[dfInfo['Particulars'] == 'Batch_no', 'Info'].values[0])

        if last_date == today:
            daily_batch += 1
        else:
            daily_batch = 1
            cursor.execute("UPDATE info_db SET Info = %s WHERE Particulars = 'Last_Date'", (today,))

        cursor.execute("UPDATE info_db SET Info = %s WHERE Particulars = 'Batch_no'", (daily_batch,))
        conn.commit()

        if plc_running:
            tags = df_Trigger_active_tag['Tag_name'].tolist()
            results, timestamp = pylogix.readABPLC(plc, tags)

            df_Trigger_active_tag['Value'] = None
            df_Trigger_active_tag['Timestamp'] = timestamp

            for ret in results:
                if ret.Status == 'Success' and ret.TagName in df_Trigger_active_tag['Tag_name'].values:
                    df_Trigger_active_tag.loc[df_Trigger_active_tag['Tag_name'] == ret.TagName, 'Value'] = ret.Value

            if df_Trigger_active_tag['Value'].isnull().any():
                raise ValueError("Null values found in PLC read.")

            category_0 = df_Trigger_active_tag.loc[
                (df_Trigger_active_tag['Name'] == "SetWeight") & 
                (df_Trigger_active_tag['Value'] == 0.0), 'Category']
            if not category_0.empty:
                df_Trigger_active_tag = df_Trigger_active_tag[
                    ~df_Trigger_active_tag['Category'].isin(category_0)]

            df_Trigger_active_tag["BatchNo"] = new_batch
            df_Trigger_active_tag["DailyBatchNo"] = daily_batch

            values = [
                (
                    row['Timestamp'], row['Name'], row['Data_type'], row['Value'],
                    row['Category'], row['BatchNo'], row['DailyBatchNo']
                )
                for _, row in df_Trigger_active_tag.iterrows()
            ]

            insert_sql = '''
                INSERT INTO plc_data ("TimeStamp", "Name", "DataType", "Value", "Category", "BatchNo", "DailyBatchNo")
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            '''
            cursor.executemany(insert_sql, values)
            conn.commit()

            # Optional: Call post-log actions
            sqliteCon.insertBatch(df_Trigger_active_tag)
            sqliteCon.insertMaterialExtraction(df_Trigger_active_tag, conn, cursor)

            df_live = df_Trigger_active_tag[['Timestamp', 'Category', 'Name', 'Data_type', 'Value']]
            logging.info(f"Data logged successfully at {timestamp}")
            return df_live

    except Exception as e:
        logging.exception(" Error in run_logging:")
        return None
