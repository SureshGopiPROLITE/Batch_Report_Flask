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
from modules.batch_summary import calculate_batch_summary

# === Logging Setup ===
logging.basicConfig(
    filename='plc_monitor.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

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



def data_process(hours, from_time, to_time):
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
        # Convert Timestamp to IST
        # -------------------------
        if "TimeStamp" in df.columns:
            df["TimeStamp"] = (
                pd.to_datetime(df["TimeStamp"], utc=True)
                .dt.tz_convert("Asia/Kolkata")
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
        # Sort latest batches first
        # -------------------------
        df = df.sort_values(
            by="BatchNo",
            ascending=False
        )

        # Rename column
        df = df.rename(
            columns={
                "Total Batch Weight": "Total Batch Weight(Kg)"
            }
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

def plc_data_process(batch_no):
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

import pandas as pd

def report_data_process(batch_no):
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
    
def dashboard_calculations(start_timestamp, end_timestamp, hours):
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
        
        
        df_prod = df_plc[df_plc["Name"] == "TotalBatchActualWeight"]
        prod_values = pd.to_numeric(df_prod["Value"], errors="coerce")
        total_production_tons = round(prod_values.sum() / 1000.0, 2) if not df_prod.empty else 0.0

        number_of_batches = int(df_batches["BatchNo"].nunique()) if "BatchNo" in df_batches.columns else 0

        elapsed_hours = time_diff_hours if time_diff_hours > 0 else 1.0
        tph = round(total_production_tons / elapsed_hours, 2)

        df_acc = df_plc[df_plc["Name"] == "BatchAccuracy"]
        batch_accuracy = round(pd.to_numeric(df_acc["Value"], errors="coerce").mean(), 2) if not df_acc.empty else 0.0

        df_cycle = df_plc[df_plc["Name"] == "BatchTimeMinutes"]
        avg_cycle_time = round(pd.to_numeric(df_cycle["Value"], errors="coerce").mean(), 2) if not df_cycle.empty else 0.0

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