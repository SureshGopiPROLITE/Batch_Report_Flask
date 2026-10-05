import logging
from config import sqliteCon
from plc_connection import pylogix
from sqlalchemy import text
from modules import Report
from datetime import datetime, timedelta
from itertools import product
import pandas as pd
import time
from flask import session
import psycopg2
from psycopg2 import sql
import pandas as pd
from modules.batch_summary import calculate_batch_summary, clean_plc_datetime, silo_time_range
from modules import shift

# === Logging Setup ===
logging.basicConfig(
    filename='plc_monitor.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


def _close(*objs):
    """Close DB connections, ignoring the ones never opened."""
    for obj in objs:
        if obj is None:
            continue
        try:
            obj.close()
        except Exception:
            pass


def df_split(dfPlcdb):
    try:
        if not dfPlcdb.loc[dfPlcdb['Sample_mode'] == "Trigger"].empty:
            dfplcdb_Periodic = dfPlcdb[dfPlcdb["Sample_mode"] == "Periodic"]
            #spliting DF for Trigger and Periodic
            unique_triggers = dfPlcdb['Trigger'].dropna().unique()
            df_trigger = dfPlcdb[dfPlcdb["Name"].isin(unique_triggers)]
            
            # Create DataFrames based on unique triggers and store them in the dictionary
            for a in unique_triggers:
                # setattr(self, a, dfPlcdb[dfPlcdb['Trigger'] == a])
                globals()[a] = dfPlcdb[dfPlcdb['Trigger'] == a]
                
        return dfPlcdb, df_trigger, dfplcdb_Periodic            

    except Exception as e:    
        print(f" ERROR: {e}")



BATCH_EXTRA_NAMES = ("Mixer Selected", "Shift", "Start Date Time", "StartTime",
                     "TotalBatchSetWeight", "TotalBatchActualWeight", "SetWeight", "ActualWeight")


def batch_extras(batch_nos, logged_at=None):
    """Per batch: Mixer No, Shift, Total Set / Actual Weight (kg), from plc_data.
    Batches logged before the summary rows existed use the sum of their silos."""
    cols = ["BatchNo", "Mixer No", "Shift", "Total Set Weight(Kg)", "Total Actual Weight(Kg)"]
    batch_nos = [int(b) for b in batch_nos]
    if not batch_nos:
        return pd.DataFrame(columns=cols)

    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
    try:
        shifts = shift.load(cursorRead)
        cursorRead.execute(
            'SELECT "BatchNo", "Name", "Value", "Category" FROM plc_data '
            'WHERE "BatchNo" = ANY(%s) AND "Name" = ANY(%s)',
            (batch_nos, list(BATCH_EXTRA_NAMES)))
        rows = pd.DataFrame(cursorRead.fetchall(), columns=["BatchNo", "Name", "Value", "Category"])
    finally:
        conn.close()

    logged_at = logged_at or {}
    out = pd.DataFrame({"BatchNo": batch_nos})
    rows["Num"] = pd.to_numeric(rows["Value"], errors="coerce")

    # One pass over all batches (a per-batch loop took > 1 min for 2 months)
    info = rows[rows["Category"] == "Info"].pivot_table(
        index="BatchNo", columns="Name", values="Value", aggfunc="last")
    summary = rows[rows["Category"] == "Summary"].pivot_table(
        index="BatchNo", columns="Name", values="Num", aggfunc="last")
    silos = rows[~rows["Category"].isin(["Info", "Summary"])].pivot_table(
        index="BatchNo", columns="Name", values="Num", aggfunc="sum")
    # Batches without a header start time: their earliest silo StartTime
    silo_rows = rows[~rows["Category"].isin(["Info", "Summary"]) & (rows["Name"] == "StartTime")]
    silo_start = (pd.to_datetime(silo_rows["Value"].map(clean_plc_datetime), errors="coerce")
                  .groupby(silo_rows["BatchNo"]).min())
    for frame, names in ((info, ("Mixer Selected", "Shift", "Start Date Time")),
                         (summary, ("TotalBatchSetWeight", "TotalBatchActualWeight")),
                         (silos, ("SetWeight", "ActualWeight"))):
        for n in names:
            if n not in frame.columns:
                frame[n] = None
    out = (out.merge(info[["Mixer Selected", "Shift", "Start Date Time"]], left_on="BatchNo", right_index=True, how="left")
              .merge(summary[["TotalBatchSetWeight", "TotalBatchActualWeight"]], left_on="BatchNo", right_index=True, how="left")
              .merge(silos[["SetWeight", "ActualWeight"]], left_on="BatchNo", right_index=True, how="left"))

    # Batches logged before the summary rows existed: sum of their silos
    set_total = pd.to_numeric(out["TotalBatchSetWeight"], errors="coerce").fillna(pd.to_numeric(out["SetWeight"], errors="coerce"))
    act_total = pd.to_numeric(out["TotalBatchActualWeight"], errors="coerce").fillna(pd.to_numeric(out["ActualWeight"], errors="coerce"))
    mixer = out["Mixer Selected"].astype(str).str.strip()
    out["Mixer No"] = mixer.where(~mixer.isin(["None", "nan", "<NA>"]), "")
    out["Shift"] = [
        shift.for_batch({"Shift": sh, "Start Date Time": st if shift.parse_time(st) else silo_start.get(b)},
                        shifts, logged_at.get(b))
        for b, sh, st in zip(out["BatchNo"], out["Shift"], out["Start Date Time"])
    ]
    out["Total Set Weight(Kg)"] = set_total.fillna(0).round(2)
    out["Total Actual Weight(Kg)"] = act_total.fillna(0).round(2)
    return out[cols]


def backfill_batch_shifts(chunk=2000):
    """Saves the shift of every batch that has none yet (batches logged before
    shifts were stored): a plc_data Info "Shift" row and "Batches"."Shift".
    Same rule as the reports: the PLC's Shift tag, else the batch start time
    (header tag or earliest silo StartTime), else the logged time.
    Returns the number of batches updated."""
    from psycopg2.extras import execute_values

    conn, cur, _ = sqliteCon.get_db_connection()
    try:
        cur.execute('SELECT "BatchNo", MIN("TimeStamp") FROM "Batches" '
                    'WHERE COALESCE("Shift", %s) = %s GROUP BY "BatchNo"', ("", ""))
        missing = {int(b): ts for b, ts in cur.fetchall() if b is not None}
    finally:
        conn.close()

    updated = 0
    batch_nos = sorted(missing)
    for i in range(0, len(batch_nos), chunk):
        part = batch_nos[i:i + chunk]
        extras = batch_extras(part, {b: missing[b] for b in part})
        values = [(int(b), str(sh)) for b, sh in zip(extras["BatchNo"], extras["Shift"])
                  if str(sh or "").strip()]
        if not values:
            continue
        conn, cur, _ = sqliteCon.get_db_connection()
        try:
            with conn:
                execute_values(cur, '''
                    WITH v("BatchNo", "Shift") AS (VALUES %s)
                    INSERT INTO plc_data ("TimeStamp", "Name", "DataType", "Value",
                                          "Category", "BatchNo", "DailyBatchNo")
                    SELECT MIN(p."TimeStamp"), 'Shift', 'STRING', v."Shift", 'Info',
                           v."BatchNo", MAX(p."DailyBatchNo")
                    FROM v JOIN plc_data p ON p."BatchNo" = v."BatchNo"
                    WHERE NOT EXISTS (SELECT 1 FROM plc_data s WHERE s."BatchNo" = v."BatchNo"
                                      AND s."Category" = 'Info' AND s."Name" = 'Shift')
                    GROUP BY v."BatchNo", v."Shift"
                ''', values, page_size=len(values))
                execute_values(cur, '''
                    UPDATE "Batches" b SET "Shift" = v."Shift"
                    FROM (VALUES %s) AS v("BatchNo", "Shift")
                    WHERE b."BatchNo" = v."BatchNo"
                ''', values, page_size=len(values))
        finally:
            conn.close()
        updated += len(values)
    return updated


def backfill_batch_summaries(chunk=1000):
    """Adds the Summary rows (totals, accuracy, batch time) to batches that have
    none - they were skipped while the PLC sent no header Start/End Date Time.
    Returns the number of batches updated."""
    from modules.batch_summary import batch_summary_rows

    conn, cur, _ = sqliteCon.get_db_connection()
    try:
        cur.execute('''
            SELECT DISTINCT p."BatchNo" FROM plc_data p
            WHERE p."BatchNo" IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM plc_data s WHERE s."BatchNo" = p."BatchNo" AND s."Category" = %s)
            ORDER BY 1''', ("Summary",))
        batch_nos = [int(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()

    updated = 0
    for i in range(0, len(batch_nos), chunk):
        part = batch_nos[i:i + chunk]
        conn, cur, _ = sqliteCon.get_db_connection()
        try:
            cur.execute('SELECT "Name", "Value", "Category", "BatchNo", "DailyBatchNo" '
                        'FROM plc_data WHERE "BatchNo" = ANY(%s)', (part,))
            rows = pd.DataFrame(cur.fetchall(),
                                columns=["Name", "Value", "Category", "BatchNo", "DailyBatchNo"])
            values = []
            for _, df in rows.groupby("BatchNo"):
                values += batch_summary_rows(df.assign(DailyBatchNo=pd.to_numeric(
                    df["DailyBatchNo"], errors="coerce").fillna(0)))
            if values:
                with conn:
                    cur.executemany(
                        'INSERT INTO plc_data ("TimeStamp", "Name", "DataType", "Value", '
                        '"Category", "BatchNo", "DailyBatchNo") VALUES (%s, %s, %s, %s, %s, %s, %s)',
                        values)
                updated += len({v[5] for v in values})
        finally:
            conn.close()
    return updated


def start_shift_backfill():
    """Runs the shift and summary backfills in the background at app start."""
    import threading

    def run():
        for label, job in (("Shift", backfill_batch_shifts), ("Summary", backfill_batch_summaries)):
            try:
                n = job()
                if n:
                    logging.info(f"{label} saved for {n} older batches")
            except Exception:
                logging.exception(f"Could not save the {label.lower()} of older batches")
    threading.Thread(target=run, daemon=True).start()


def data_process(hours, from_time, to_time):
    conn = engineConRead = engineConWrite = None
    try:
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()

        df = sqliteCon.data_batch(
            conn,
            hours,
            from_time,
            to_time,
            engineConRead
        )

        if df is None or df.empty:
            return {
                "success": True,
                "data": [],
                "total_weight": 0.0
            }

        # -------------------------
        # Keep only required columns
        # -------------------------
        column_order = [
            "BatchNo",
            "TimeStamp",
            "Plant Name",
            "Recipe Name",
            "Start Date Time",
            "End Date Time",
            "Total Batch Weight"
        ]

        existing_columns = [c for c in column_order if c in df.columns]
        df = df[existing_columns].copy()

        # -------------------------
        # TimeStamp is saved in local time already (datetime.now() under the
        # TZ set in docker-compose) - format only, no timezone conversion
        # -------------------------
        if "TimeStamp" in df.columns:
            df["TimeStamp"] = (
                pd.to_datetime(df["TimeStamp"], errors="coerce")
                .dt.strftime("%Y-%m-%d %H:%M:%S")
            )

        # -------------------------
        # Convert numeric columns
        # -------------------------
        if "BatchNo" in df.columns:
            df["BatchNo"] = (
                pd.to_numeric(df["BatchNo"], errors="coerce")
                .fillna(0)
                .astype(int)
            )

        if "Total Batch Weight" in df.columns:
            df["Total Batch Weight"] = (
                pd.to_numeric(df["Total Batch Weight"], errors="coerce")
                .fillna(0)
            )

        # -------------------------
        # Calculate total weight
        # -------------------------
        total_weight_tons = round(
            df["Total Batch Weight"].sum() / 1000,
            2
        )

        # -------------------------
        # Mixer No, Shift, Total Set / Actual Weight per batch
        # (searchable in the report and included in the Export)
        # -------------------------
        logged = dict(zip(df["BatchNo"], df["TimeStamp"])) if "TimeStamp" in df.columns else {}
        df = df.merge(batch_extras(df["BatchNo"].tolist(), logged), on="BatchNo", how="left")
        df = df.drop(columns=[c for c in ("Total Batch Weight",) if c in df.columns])

        # -------------------------
        # Sort latest batches first
        # -------------------------
        df = df.sort_values(
            by="BatchNo",
            ascending=False
        )

        # -------------------------
        # Convert remaining object columns to string
        # -------------------------
        for col in df.columns:
            if df[col].dtype == "object":
                df[col] = df[col].fillna("").astype(str)

        return {
            "success": True,
            "data": df.to_dict(orient="records"),
            "total_weight": total_weight_tons
        }

    except Exception as e:
        print(f" Error in data_process: {e}")
        import traceback
        traceback.print_exc()

        return {
            "success": False,
            "error": str(e)
        }

    finally:
        _close(conn, engineConRead, engineConWrite)


def plc_data_process(batch_no):
    conn = engineConRead = engineConWrite = None
    try:
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()

        query = 'SELECT * FROM plc_data WHERE "BatchNo" = %s'
        df = pd.read_sql_query(query, engineConRead, params=(batch_no,))

        if df.empty:
            return pd.DataFrame()

        # Separate Info category
        df_string = df[df["Category"] == "Info"].copy()
        df = df[df["Category"] != "Info"].copy()

        df_pivot = df.pivot(index="Category", columns="Name", values="Value")

        original_order = df["Name"].unique()
        df_pivot = df_pivot[original_order].reset_index()

        df_pivot["Category_numeric"] = (
            df_pivot["Category"]
            .str.extract(r"Silo-(\d+)")
            .astype(float)
        )

        df_pivot = (
            df_pivot.sort_values("Category_numeric")
            .drop(columns="Category_numeric")
            .reset_index(drop=True)
        )

        # -----------------------------
        # Convert numeric columns
        # -----------------------------
        numeric_columns = [
            "SetWeight",
            "ActualWeight",
            "InflightWeight",
            "Tolerance",
            "CoarseSpeed",
            "FineSpeed",
            "SiloNo",
        ]

        for col in numeric_columns:
            if col in df_pivot.columns:
                df_pivot[col] = pd.to_numeric(df_pivot[col], errors="coerce")

        # Difference
        df_pivot["Difference"] = df_pivot.apply(
            lambda row: Report.difference(
                row["SetWeight"],
                row["ActualWeight"]
            ),
            axis=1,
        )

        # Daily Batch Number
        query_daily = '''
            SELECT DISTINCT "DailyBatchNo"
            FROM plc_data
            WHERE "BatchNo" = %s
        '''

        df_daily = pd.read_sql_query(
            query_daily,
            engineConRead,
            params=(batch_no,),
        )

        DailyBatchNo = (
            df_daily.iloc[0]["DailyBatchNo"]
            if not df_daily.empty
            else None
        )

        print(f"BatchNo={batch_no}, DailyBatchNo={DailyBatchNo}")

        # State calculation
        state_dict = df_pivot.apply(
            lambda row: Report.check(
                row["SetWeight"],
                row["ActualWeight"],
                row["Tolerance"],
            ),
            axis=1,
        )

        column_order = [
            "Category",
            "SiloNo",
            "MaterialName",
            "SetWeight",
            "ActualWeight",
            "Difference",
            "Tolerance",
            "CoarseSpeed",
            "FineSpeed",
        ]

        df_pivot = df_pivot[column_order]

        if "SiloNo" in df_pivot.columns:
            df_pivot["SiloNo"] = (
                df_pivot["SiloNo"]
                .fillna(0)
                .astype(int)
            )

        return df_pivot

    except Exception as e:
        print(f" Error in plc_data_process: {e}")
        return pd.DataFrame()

    finally:
        _close(conn, engineConRead, engineConWrite)



def add_material_times(df_pivot):
    """Per-silo StartTime / EndTime (shown as HH:MM:SS) and their Duration.
    Batches logged before these tags existed get empty values."""
    for col in ("StartTime", "EndTime"):
        if col not in df_pivot.columns:
            df_pivot[col] = None

    start = pd.to_datetime(df_pivot["StartTime"].map(clean_plc_datetime), errors="coerce")
    end = pd.to_datetime(df_pivot["EndTime"].map(clean_plc_datetime), errors="coerce")

    def fmt_duration(seconds):
        if pd.isna(seconds) or seconds < 0:
            return ""
        seconds = int(seconds)
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"

    df_pivot["Duration"] = (end - start).dt.total_seconds().map(fmt_duration)
    df_pivot["StartTime"] = start.dt.strftime("%H:%M:%S").fillna("")
    df_pivot["EndTime"] = end.dt.strftime("%H:%M:%S").fillna("")
    return df_pivot


def report_data_process(batch_no):
    conn = engineConRead = engineConWrite = None
    try:
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()

        query = 'SELECT * FROM plc_data WHERE "BatchNo" = %s'

        df = pd.read_sql_query(
            query,
            engineConRead,
            params=(batch_no,),
        )

        if df.empty:
            return (
                pd.DataFrame(),
                pd.DataFrame(),
                None,
                pd.DataFrame(),
            )

        df_string = df[df["Category"] == "Info"].copy()

        df_cal_sum = df[df["Category"] == "Summary"].copy()

        numeric_vals = pd.to_numeric(
            df_cal_sum["Value"],
            errors="coerce",
        )

        df_cal_sum["Value"] = numeric_vals.round(2).astype(str).where(
            ~numeric_vals.isna(),
            df_cal_sum["Value"],
        )

        df = df[
            ~df["Category"].isin(["Info", "Summary"])
        ].copy()

        df_pivot = df.pivot(
            index="Category",
            columns="Name",
            values="Value",
        )

        original_order = df["Name"].unique()

        df_pivot = df_pivot[original_order].reset_index()

        df_pivot["Category_numeric"] = (
            df_pivot["Category"]
            .str.extract(r"Silo-(\d+)")
            .astype(float)
        )

        df_pivot = (
            df_pivot.sort_values("Category_numeric")
            .drop(columns="Category_numeric")
            .reset_index(drop=True)
        )

        # -----------------------------
        # Convert numeric columns
        # -----------------------------
        numeric_columns = [
            "SetWeight",
            "ActualWeight",
            "Tolerance",
            "CoarseSpeed",
            "FineSpeed",
            "SiloNo",
        ]

        for col in numeric_columns:
            if col in df_pivot.columns:
                df_pivot[col] = pd.to_numeric(df_pivot[col], errors="coerce")

        df_pivot["Difference"] = df_pivot.apply(
            lambda row: Report.difference(
                row["SetWeight"],
                row["ActualWeight"],
            ),
            axis=1,
        )

        query_daily = '''
            SELECT DISTINCT "DailyBatchNo"
            FROM plc_data
            WHERE "BatchNo" = %s
        '''

        df_daily = pd.read_sql_query(
            query_daily,
            engineConRead,
            params=(batch_no,),
        )

        daily_batch_no = (
            df_daily.iloc[0]["DailyBatchNo"]
            if not df_daily.empty
            else None
        )

        column_order = [
            "Category",
            "SiloNo",
            "MaterialName",
            "SetWeight",
            "ActualWeight",
            "Difference",
            "Tolerance",
            "InflightWeight",
            "CoarseSpeed",
            "FineSpeed",
            "StartTime",
            "EndTime",
            "Duration",
        ]

        df_pivot = add_material_times(df_pivot)
        # Batches logged before a tag existed (e.g. InflightWeight) get empty cells
        df_pivot = df_pivot.reindex(columns=column_order)

        if "SiloNo" in df_pivot.columns:
            df_pivot["SiloNo"] = (
                df_pivot["SiloNo"]
                .fillna(0)
                .astype(int)
            )

        # Batches saved without header Start/End Date Time: use the silo times
        silo_start, silo_end = silo_time_range(df)
        have = {} if df_string.empty else dict(zip(df_string["Name"], df_string["Value"]))
        extra = [{"Name": name, "Value": value, "Category": "Info", "BatchNo": batch_no}
                 for name, value in (("Start Date Time", silo_start), ("End Date Time", silo_end))
                 if value is not None and clean_plc_datetime(have.get(name)) is None]
        if extra:
            df_string = df_string[~df_string["Name"].isin([e["Name"] for e in extra])]
            df_string = pd.concat([df_string, pd.DataFrame(extra)], ignore_index=True)

        # Shift for popup / PDF / Excel: the PLC's Shift tag if it sends one,
        # otherwise from the batch start time and the Settings shift times
        if df_string.empty or "Shift" not in set(df_string["Name"]):
            shifts = shift.load(cursorRead)
            info = dict(zip(df_string["Name"], df_string["Value"])) if not df_string.empty else {}
            logged = df_string["TimeStamp"].min() if "TimeStamp" in df_string.columns and not df_string.empty else None
            df_string = pd.concat([df_string, pd.DataFrame([{
                "Name": "Shift", "Value": shift.for_batch(info, shifts, logged),
                "Category": "Info", "BatchNo": batch_no,
            }])], ignore_index=True)

        return (
            df_pivot,
            df_string,
            daily_batch_no,
            df_cal_sum,
        )

    except Exception as e:
        print(f" Error in report_data_process: {e}")

        return (
            pd.DataFrame(),
            pd.DataFrame(),
            None,
            pd.DataFrame(),
        )

    finally:
        _close(conn, engineConRead, engineConWrite)


def dashboard_calculations(start_timestamp, end_timestamp, hours):
    conn = engineConRead = engineConWrite = None
    try:
        
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()
      
        if hours == "Custom":

            from_time_dt = pd.to_datetime(start_timestamp)
            to_time_dt = pd.to_datetime(end_timestamp)

        elif hours in ["1 Hr", "4 Hr", "8 Hr", "12 Hr", "24 Hr"]:

            hours_mapping = {
                "1 Hr": 1,
                "4 Hr": 4,
                "8 Hr": 8,
                "12 Hr": 12,
                "24 Hr": 24
            }

            to_time_dt = datetime.now()
            from_time_dt = to_time_dt - timedelta(hours=hours_mapping[hours])

        else:
            print("Invalid hours option")
            return {
                "status": "success",
                "summary": {},
                "line_chart": [],
                "recipe_chart": [],
                "raw_material_chart": [],
                "calendar_chart": []
            }

        from_time_sql = from_time_dt.strftime("%Y-%m-%d %H:%M:%S")
        to_time_sql = to_time_dt.strftime("%Y-%m-%d %H:%M:%S")

        print("From :", from_time_sql)
        print("To   :", to_time_sql)

        # if hours != "Custom":
        #     print("No batch data found in range")
        #     return {
        #         "status": "success",
        #         "summary": {},
        #         "line_chart": [],
        #         "recipe_chart": [],
        #         "raw_material_chart": [],
        #         "calendar_chart": []
        #     }

        # Ensure datetimes
        start_dt = pd.to_datetime(from_time_sql)
        end_dt = pd.to_datetime(to_time_sql)
        time_diff_hours = (end_dt - start_dt).total_seconds() / 3600.0
        print(f" Time difference in hours: {time_diff_hours}")

        # --------------------- PLC DATA ---------------------
        query_plc = 'SELECT * FROM plc_data WHERE "TimeStamp" BETWEEN %s AND %s'
        df_plc = pd.read_sql_query(query_plc, engineConRead, params=(start_dt, end_dt))

        if df_plc.empty:
            print("No PLC data found in range")
            return {
                "status": "success",
                "summary": {},
                "line_chart": [],
                "recipe_chart": [],
                "raw_material_chart": [],
                "calendar_chart": []
            }

        # --------------------- BATCH LOGS ---------------------
      
        query_batches = 'SELECT * FROM "Batches" WHERE "TimeStamp" BETWEEN %s AND %s'
        df_batches = pd.read_sql_query(query_batches, engineConRead, params=(start_dt, end_dt))

        df_ttl_tons = sqliteCon.show_data(conn, hours, str(start_dt), str(end_dt), engineConRead)

        if df_ttl_tons is None or df_ttl_tons.empty:
            return {
                "status": "success",
                "summary": {},
                "line_chart": [],
                "recipe_chart": [],
                "raw_material_chart": [],
                "calendar_chart": []
            }
        # Use your existing processing function
        df_diff = sqliteCon.process_batch_data(df_ttl_tons)
        if df_diff is None or df_diff.empty:
            return {
                "status": "success",
                "summary": {},
                "line_chart": [],
                "recipe_chart": [],
                "raw_material_chart": [],
                "calendar_chart": []
            }
        

        # Keep only the columns your frontend expects (if present)
        column_order = ["Category", "SetWeight", "ActualWeight", "Error_%", "Error_Kg"]
        existing_columns = [c for c in column_order if c in df_diff.columns]
        df_diff = df_diff[existing_columns]

        # Calculate total (sum Error_Kg -> convert to tons by dividing 1000)
        total_error_kg = df_diff["Error_Kg"].sum() if "Error_Kg" in df_diff.columns else 0
        total_tons = round(total_error_kg / 1000.0, 2)


        # Full table for calendar chart
        query_calander = 'SELECT * FROM "Batches"'
        df_calander = pd.read_sql_query(query_calander, engineConRead)

        if df_batches.empty:
            print("No batch data found in range")
            return {
                "status": "success",
                "summary": {},
                "line_chart": [],
                "recipe_chart": [],
                "raw_material_chart": [],
                "calendar_chart": []
            }

        # ------------------ LINE CHART LOGIC (MULTI PLANT) ------------------
              

        df_batches["TimeStamp"] = pd.to_datetime(
            df_batches["TimeStamp"],
            errors="coerce"
        )

        # Hourly chart for 24 hours or less
        if time_diff_hours <= 24:

            df_batches["TimeKey"] = (
                df_batches["TimeStamp"]
                .dt.floor("h")
            )

        else:

            # Daily chart
            df_batches["TimeKey"] = (
                df_batches["TimeStamp"]
                .dt.date
            )

        grouped = (
            df_batches
            .groupby(["TimeKey", "Plant Name"])["BatchNo"]
            .nunique()
            .reset_index(name="BatchCount")
            .sort_values("TimeKey")
        )

        # Convert to string for JSON
        grouped["TimeKey"] = grouped["TimeKey"].astype(str)

        line_chart = grouped.to_dict(orient="records")

        print("Grouped counts (preview):")
        print(grouped.head(20))


        # ---------------------- SUMMARY -----------------------
        
        
        # Production: the batches logged in the range (same source and total as
        # the Report page), one row per batch
        batches_once = df_batches.drop_duplicates(subset="BatchNo")
        total_production_tons = round(
            pd.to_numeric(batches_once["Total Batch Weight"], errors="coerce").sum() / 1000.0, 2)

        number_of_batches = int(batches_once["BatchNo"].nunique())

        elapsed_hours = time_diff_hours if time_diff_hours > 0 else 1.0
        tph = round(total_production_tons / elapsed_hours, 2)

        # Accuracy / cycle time of those same batches. Summary rows carry the
        # PLC end time, so select them by batch number, not by their timestamp.
        cursorRead.execute(
            'SELECT "BatchNo", "Name", "Value" FROM plc_data WHERE "Category" = %s '
            'AND "Name" IN (%s, %s) AND "BatchNo" = ANY(%s)',
            ("Summary", "BatchAccuracy", "BatchTimeMinutes",
             [int(b) for b in batches_once["BatchNo"].dropna()]))
        df_summary = pd.DataFrame(cursorRead.fetchall(), columns=["BatchNo", "Name", "Value"])
        df_summary["Value"] = pd.to_numeric(df_summary["Value"], errors="coerce")
        df_summary = df_summary.drop_duplicates(subset=["BatchNo", "Name"], keep="last")

        def summary_mean(name):
            value = df_summary.loc[df_summary["Name"] == name, "Value"].mean()
            return 0.0 if pd.isna(value) else round(float(value), 2)

        batch_accuracy = summary_mean("BatchAccuracy")
        avg_cycle_time = summary_mean("BatchTimeMinutes")

        # --------------------- RAW MATERIAL -------------------
        df_filtered = df_plc[~df_plc["Category"].isin(["Info", "Summary"])]
        weights_df = df_filtered[df_filtered["Name"].isin(["ActualWeight", "SetWeight"])].copy()

        if not weights_df.empty:
            weights_df["Value"] = pd.to_numeric(weights_df["Value"], errors="coerce")
            pivot_bar = (
                weights_df.pivot_table(
                    index="Category", 
                    columns="Name", 
                    values="Value",
                    aggfunc="mean", 
                    fill_value=0
                ).reset_index()
            )
        else:
            pivot_bar = pd.DataFrame(columns=["Category", "ActualWeight", "SetWeight"])

        # --------------------- RECIPE CHART -------------------
        recipe_df = df_plc[df_plc["Name"] == "Recipe Name"]

        if not recipe_df.empty:
            recipe_counts = recipe_df["Value"].value_counts().reset_index()
            recipe_counts.columns = ["RecipeName", "Count"]
        else:
            recipe_counts = pd.DataFrame(columns=["RecipeName", "Count"])

        # --------------------- CALENDAR CHART -------------------
        df_calander["TimeStamp"] = pd.to_datetime(df_calander["TimeStamp"], errors="coerce")
        df_calander = df_calander.dropna(subset=["TimeStamp"])

        # Group by only DATE
        df_calander["DateOnly"] = df_calander["TimeStamp"].dt.date

        calendar_group = (
            df_calander.groupby("DateOnly")["BatchNo"]
            .nunique()
            .reset_index(name="Value")
            .sort_values("DateOnly")
        )

        # Format for frontend
        calendar_chart = [
            {"date": str(row["DateOnly"]), "value": int(row["Value"])}
            for _, row in calendar_group.iterrows()
        ]

        # --------------------- FINAL JSON ---------------------
        return {
            "status": "success",
            "summary": {
                "total_production_tons": total_production_tons,
                "num_batches": number_of_batches,
                "tph": tph,
                "batch_accuracy": batch_accuracy,
                "avg_cycle_time": avg_cycle_time,
                "total_loss": total_tons
            },
            "line_chart": grouped.to_dict(orient="records"),
            "recipe_chart": recipe_counts.to_dict(orient="records"),
            "raw_material_chart": pivot_bar.to_dict(orient="records"),
            "calendar_chart": calendar_chart    
        }

    except Exception as e:
        print("Error:", e)
        return {"status": "error", "message": str(e)}

    finally:
        _close(conn, engineConRead, engineConWrite)

