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


def clean_plc_datetime(date_string):
    if pd.isna(date_string):
        return None

    date_string = re.sub(r"\s*([:-])\s*", r"\1", str(date_string).strip())  # removes spaces around - and :
    date_string = re.sub(r"\s+", " ", date_string)  # compresses multiple spaces into a single space

    dt = pd.to_datetime(date_string, errors="coerce")  # dt = Timestamp (a single datetime value)

    return None if pd.isna(dt) else dt.strftime("%Y-%m-%d %H:%M:%S")


def calculate_batch_summary(dfPlcdb):

    if dfPlcdb.empty:
        return

    # Was: sqlite3.connect(DB) — but the query below already used %s
    # placeholders, which sqlite3 does NOT support (it only understands
    # ? or :name), so this was broken even before the Postgres move.
    # Now uses the shared Postgres connection, consistent with the rest
    # of the app (same pattern as monitor.py's Sqlite.sqlite() calls).
    cursorRead, cursor, engineConRead, engineConWrite, conn = postgres.postgres()

    try:
        df = dfPlcdb.copy()

        df["Value_num"] = pd.to_numeric(df["Value"], errors="coerce")

        batch_no = int(df["BatchNo"].iloc[0])
        daily_batch_no = int(df["DailyBatchNo"].iloc[0])

        actual_total = df.loc[df["Name"] == "ActualWeight", "Value_num"].sum()
        set_total = df.loc[df["Name"] == "SetWeight", "Value_num"].sum()

        # -----------------------------
        # Read PLC Date
        # -----------------------------
        start_value = df.loc[df["Name"] == START_NAME, "Value"].iloc[-1]
        end_value = df.loc[df["Name"] == END_NAME, "Value"].iloc[-1]

        print("--------------------------------------")
        print("---> Batch No    :", batch_no)
        print("---> Start Value :", start_value)
        print("---> End Value   :", end_value)

        start_value = clean_plc_datetime(start_value)
        end_value = clean_plc_datetime(end_value)

        if start_value is None:
            print("❌ Invalid Start Date Time")
            return

        if end_value is None:
            print("❌ Invalid End Date Time")
            return

        try:
            start = pd.to_datetime(start_value)
            end = pd.to_datetime(end_value)
        except Exception as e:
            print("❌ Date Parse Error:", e)
            return

        batch_time = round((end - start).total_seconds() / 60, 2)

        accuracy = batch_accuracy(actual_total, set_total)

        summary = {
            "TotalBatchActualWeight": actual_total,
            "TotalBatchSetWeight": set_total,
            "BatchAccuracy": accuracy,
            "BatchTimeMinutes": batch_time
        }

        timestamp = end.strftime("%Y-%m-%d %H:%M:%S")

        # "TimeStamp","Name","DataType","Value","Category","BatchNo",
        # "DailyBatchNo" are mixed-case identifiers and must be quoted or
        # Postgres will fold them to lowercase and fail to find them.
        # plc_data itself is already lowercase, so it doesn't need quoting.
        for name, value in summary.items():
            cursor.execute("""
                INSERT INTO plc_data
                (
                    "TimeStamp",
                    "Name",
                    "DataType",
                    "Value",
                    "Category",
                    "BatchNo",
                    "DailyBatchNo"
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                timestamp,
                name,
                "NUMBER",
                value,
                "Summary",
                batch_no,
                daily_batch_no
            ))

        conn.commit()

        print(f"✅  Summary inserted for Batch {batch_no}")
        print("--------------------------------------")

    finally:
        for c in (cursorRead, cursor, conn):
            try:
                c.close()
            except Exception:
                pass