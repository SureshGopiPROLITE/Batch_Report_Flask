import pandas as pd
import re
from database import postgres

START_NAME = "Start Date Time"
END_NAME = "End Date Time"


def batch_accuracy(actual, target):
    if target == 0:
        return 0

    deviation = abs(actual - target) / target * 100
    return round(100 - deviation, 2)

import pandas as pd
import re

def clean_plc_datetime(date_string):

    if pd.isna(date_string):
        return None

    date_string = str(date_string).strip()

    # Remove extra spaces around ':' and '-'
    date_string = re.sub(r"\s*([:-])\s*", r"\1", date_string)

    # Replace multiple spaces with a single space
    date_string = re.sub(r"\s+", " ", date_string)

    dt = pd.to_datetime(date_string, errors="coerce")

    if pd.isna(dt):
        return None

    return dt.strftime("%Y-%m-%d %H:%M:%S")

def calculate_batch_summary(dfPlcdb):

    if dfPlcdb.empty:
        return

    cursorRead, cursor, engineConRead, engineConWrite, conn = postgres.postgres()

    try:
        df = dfPlcdb.copy()

        # Convert PLC values to numeric
        df["Value_num"] = pd.to_numeric(df["Value"], errors="coerce")

        batch_no = int(df["BatchNo"].iloc[0])
        daily_batch_no = int(df["DailyBatchNo"].iloc[0])

        actual_total = float(df.loc[df["Name"] == "ActualWeight", "Value_num"].sum())
        set_total = float(df.loc[df["Name"] == "SetWeight", "Value_num"].sum())

        # -----------------------------
        # Read PLC Date
        # -----------------------------
        start_value = df.loc[df["Name"] == START_NAME, "Value"].iloc[-1]
        end_value = df.loc[df["Name"] == END_NAME, "Value"].iloc[-1]


        start_value = clean_plc_datetime(start_value)
        end_value = clean_plc_datetime(end_value)

        if start_value is None:
            print(" Invalid Start Date Time")
            return

        if end_value is None:
            print(" Invalid End Date Time")
            return

        start = pd.to_datetime(start_value, errors="coerce")
        end = pd.to_datetime(end_value, errors="coerce")

        if pd.isna(start) or pd.isna(end):
            print(" Invalid datetime")
            return

        batch_time = float(round((end - start).total_seconds() / 60, 2))
        accuracy = float(batch_accuracy(actual_total, set_total))

        summary = {
            "TotalBatchActualWeight": actual_total,
            "TotalBatchSetWeight": set_total,
            "BatchAccuracy": accuracy,
            "BatchTimeMinutes": batch_time
        }

        timestamp = end.strftime("%Y-%m-%d %H:%M:%S")

        # -----------------------------
        # Insert Summary
        # -----------------------------
        for name, value in summary.items():

            # Convert numpy values to Python values
            if hasattr(value, "item"):
                value = value.item()

            if pd.isna(value):
                value = None
            elif value is not None:
                value = float(value)

            cursor.execute(
                """
                INSERT INTO "plc_data"
                (
                    "TimeStamp",
                    "Name",
                    "DataType",
                    "Value",
                    "Category",
                    "BatchNo",
                    "DailyBatchNo"
                )
                VALUES
                (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                """,
                (
                    timestamp,
                    name,
                    "NUMBER",
                    value,
                    "Summary",
                    batch_no,
                    daily_batch_no,
                ),
            )

        conn.commit()

        print("-------------------------------------------------------")
        print(f" Summary inserted successfully for Batch {batch_no}")
        print("-------------------------------------------------------")

    except Exception as e:
        conn.rollback()
        print(" Error in calculate_batch_summary:", e)
        raise

    finally:
        postgres.close_postgres(cursorRead, cursor, engineConRead, engineConWrite, conn)