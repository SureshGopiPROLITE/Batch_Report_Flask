import logging
from config import sqliteCon
from plc_connection import pylogix
from sqlalchemy import text
from modules import Report

from itertools import product
from datetime import datetime
import pandas as pd
import time
from flask import session
import psycopg2
from psycopg2 import sql
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
        df = sqliteCon.data_batch(conn, hours, from_time, to_time, engineConRead)

        if df is not None and not df.empty:
            # Define and filter columns
            column_order = [
                "BatchNo", "TimeStamp", "Plant Name", "Recipe Name",
                "Start Date Time", "End Date Time", "Total Batch Weight"
            ]
            existing_columns = [col for col in column_order if col in df.columns]
            df = df[existing_columns]

            # Convert to JSON-safe types
            df = df.astype(str)
            df["BatchNo"] = df["BatchNo"].astype(int)
            # Sort by column 'Score' in descending order
            df_sorted = df.sort_values(by='BatchNo', ascending=False)
            df_sorted = df_sorted.rename(
                columns={"Total Batch Weight": "Total Batch Weight(Kg)"}
            )


            total_weight_tons = round(float(df["Total Batch Weight"].astype(float).sum() / 1000), 2)

            return {
                "success": True,
                "data": df_sorted.to_dict(orient="records"),
                "total_weight": total_weight_tons
            }

        return {"success": True, "data": [], "total_weight": 0.0}

    except Exception as e:
        print(f"❌ Error in data_process: {e}")
        return {"success": False, "error": str(e)}

def plc_data_process(batch_no):
    try:
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()
        # Query database for plc_data of selected BatchNo
        query = f"""
            SELECT *
            FROM plc_data
            WHERE BatchNo == '{batch_no}'
        """
        df = pd.read_sql_query(query, engineConRead)
        

        if df.empty:
            return {"success": True, "data": []}
        # Separate 'Info' category and transform data
        df_string = df[df["Category"] == "Info"].copy()
        df = df[df["Category"] != "Info"].copy()
        # print(df_string)
        df_pivot = df.pivot(index='Category', columns='Name', values='Value')

        # Maintain column order consistency
        original_order = df['Name'].unique()
        df_pivot = df_pivot[original_order].reset_index()

        # Sort by numerical silo category if present
        df_pivot['Category_numeric'] = df_pivot['Category'].str.extract(r'Silo-(\d+)').astype(float)
        df_pivot = df_pivot.sort_values('Category_numeric').drop('Category_numeric', axis=1).reset_index(drop=True)

        # Compute 'Difference' column
        df_pivot["Difference"] = df_pivot.apply(lambda row: Report.difference(row["SetWeight"], row["ActualWeight"]), axis=1)

        # Retrieve DailyBatchNo
        query_daily_batch = f"""
            SELECT DISTINCT DailyBatchNo
            FROM plc_data
            WHERE BatchNo == '{batch_no}'
        """
        df_daily_batch = pd.read_sql_query(query_daily_batch, engineConRead)
        # print(df_daily_batch)
        # Ensure DailyBatchNo exists and is assigned
        if not df_daily_batch.empty:
            DailyBatchNo = df_daily_batch.iloc[0]['DailyBatchNo']
            print(f"BatchNo for {batch_no}: {DailyBatchNo}")
        else:
            DailyBatchNo = None
            print(f"No DailyBatchNo found for BatchNo: {batch_no}")

        # Compute "State" but do not add it to df
        state_dict = df_pivot.apply(lambda row: Report.check(row["SetWeight"], row["ActualWeight"], row["Tolerance"]), axis=1)

        # Define displayed columns (excluding "State")
        column_order = ["Category", "SiloNo", "MaterialName", "SetWeight", "ActualWeight", "Difference", "Tolerance"]
        df_pivot = df_pivot[column_order]  # Only include these columns in the table
        df_pivot["SiloNo"] = df_pivot["SiloNo"].astype(int)

        return df_pivot

    except Exception as e:
        print(f"❌ Error in plc_data_process: {e}")
        return {"success": False, "error": str(e)}

def report_data_process(batch_no):
    try:
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()

        query = f"SELECT * FROM plc_data WHERE BatchNo = '{batch_no}'"
        df = pd.read_sql_query(query, engineConRead)

        if df.empty:
            return pd.DataFrame(), pd.DataFrame(), None

        df_string = df[df["Category"] == "Info"].copy()
        

        df_cal_sum = df[df["Category"] == "Summary"].copy()

        # Convert only numeric values
        numeric_vals = pd.to_numeric(df_cal_sum["Value"], errors="coerce")

        # Round numeric ones to 2 decimals and keep date/time as original
        df_cal_sum["Value"] = numeric_vals.round(2).astype(str).where(
            ~numeric_vals.isna(),  # if numeric → keep rounded
            df_cal_sum["Value"]    # if non-numeric (date/time) → keep original
        )

        # Remove Info and Summary rows
        df = df[~df["Category"].isin(["Info", "Summary"])].copy()


        df_pivot = df.pivot(index='Category', columns='Name', values='Value')
        original_order = df['Name'].unique()
        df_pivot = df_pivot[original_order].reset_index()

        df_pivot['Category_numeric'] = df_pivot['Category'].str.extract(r'Silo-(\d+)').astype(float)
        df_pivot = df_pivot.sort_values('Category_numeric').drop('Category_numeric', axis=1).reset_index(drop=True)

        df_pivot["Difference"] = df_pivot.apply(lambda r: Report.difference(r["SetWeight"], r["ActualWeight"]), axis=1)

        query_daily = f"SELECT DISTINCT DailyBatchNo FROM plc_data WHERE BatchNo = '{batch_no}'"
        df_daily = pd.read_sql_query(query_daily, engineConRead)
        daily_batch_no = df_daily.iloc[0]['DailyBatchNo'] if not df_daily.empty else None

        # Define displayed columns (excluding "State")
        column_order = ["Category", "SiloNo", "MaterialName", "SetWeight", "ActualWeight", "Difference", "Tolerance"]
        df_pivot = df_pivot[column_order]  # Only include these columns in the table
        df_pivot["SiloNo"] = df_pivot["SiloNo"].astype(int)
        
        return df_pivot, df_string, daily_batch_no, df_cal_sum
    except Exception as e:
        print(f"❌ Error in plc_data_process: {e}")
        return {"success": False, "error": str(e)}


def dashboard_calculations(start_timestamp, end_timestamp, hours):
    try:
        print("Dashboard calculations called")
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()

        if hours != "Custom":
            print("No batch data found in range")
            return {
                "status": "success",
                "summary": {},
                "line_chart": [],
                "recipe_chart": [],
                "raw_material_chart": [],
                "calendar_chart": []
            }
        

        # Ensure datetimes
        start_dt = pd.to_datetime(start_timestamp)
        end_dt = pd.to_datetime(end_timestamp)
        time_diff_hours = (end_dt - start_dt).total_seconds() / 3600.0
        print(f" Time difference in hours: {time_diff_hours}")

        # --------------------- PLC DATA ---------------------
        query_plc = f"""
            SELECT *
            FROM plc_data
            WHERE TimeStamp BETWEEN '{start_dt}' AND '{end_dt}'
        """
        df_plc = pd.read_sql_query(query_plc, engineConRead)

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
        query_batches = f"""
            SELECT *
            FROM Batches
            WHERE TimeStamp BETWEEN '{start_dt}' AND '{end_dt}'
        """
        df_batches = pd.read_sql_query(query_batches, engineConRead)

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
        query_calander = "SELECT * FROM Batches"
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
        df_batches["TimeStamp"] = pd.to_datetime(df_batches["TimeStamp"], errors="coerce")

        if time_diff_hours < 24:
            df_batches["TimeKey"] = df_batches["TimeStamp"].dt.floor("H")
            time_fmt = "%H:00"
        else:
            df_batches["TimeKey"] = df_batches["TimeStamp"].dt.date
            time_fmt = "%Y-%m-%d"

        grouped = (
            df_batches
            .groupby(["TimeKey", "Plant Name"])["BatchNo"]
            .nunique()
            .reset_index(name="BatchCount")
            .sort_values("TimeKey")
        )

        grouped["TimeKey"] = grouped["TimeKey"].astype(str)

        line_chart = grouped.to_dict(orient="records")


        print("Grouped counts (preview):")
        print(grouped.head(10))

        # ---------------------- SUMMARY -----------------------
        df_prod = df_plc[df_plc["Name"] == "TotalBatchActualWeight"]
        total_production_tons = round(df_prod["Value"].sum() / 1000.0, 2) if not df_prod.empty else 0.0

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
            "calendar_chart": calendar_chart     # <---- ADDED HERE
        }

    except Exception as e:
        print("Error:", e)
        return {"status": "error", "message": str(e)}









        

        

        
        