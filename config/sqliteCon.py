import psycopg2
from psycopg2.extras import DictCursor
from sqlalchemy import create_engine
import pandas as pd
from datetime import datetime, time, timedelta
from config.config import DB_CONFIG


# === Direct psycopg2 Connection (for raw cursor use) ===
def get_db_connection():
    try:
        conn = psycopg2.connect(
            dbname=DB_CONFIG['dbname'],
            user=DB_CONFIG['user'],
            password=DB_CONFIG['password'],
            host=DB_CONFIG['host'],
            port=DB_CONFIG['port'],
            cursor_factory=DictCursor
        )
        cursorRead = conn.cursor()
        cursorWrite = conn.cursor()
        return conn, cursorRead, cursorWrite
    except Exception as e:
        print(f"Failed to connect to PostgreSQL: {e}")
        return None

0.
# === SQLAlchemy Engine for pandas.to_sql and read_sql ===
def get_db_connection_engine():
    try:
        db_url = (
            f"postgresql+psycopg2://{DB_CONFIG['user']}:{DB_CONFIG['password']}@"
            f"{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}"
        )
        engine = create_engine(db_url)
        engineConRead = engine.connect()
        engineConWrite = engine.connect()
        print(" Postgres SQLAlchemy engine created successfully.")
        return engine, engineConRead, engineConWrite
    except Exception as e:
        print(f" Failed to create SQLAlchemy engine: {e}")
        return None, None, None


# === Example Function to Read Users Table ===
def dfUser():
    engine, engineConRead, engineConWrite = get_db_connection_engine()
    query = "SELECT id, username, role, is_active, last_login FROM users WHERE role != 'superadmin';"
    df = pd.read_sql_query(query, engineConRead)
    df.columns = ['Id', 'Username', 'Role', 'Is_Active', 'LastLogin']
    df = df.sort_values(by='Id', ascending=True).reset_index(drop=True)

    # Add UI numbering (starting from 1)
    df.insert(0, 'UID', df.index + 1)

    return df


# === Insert Batch Function ===
def insertBatch(df):

    engine, engineConRead, engineConWrite = get_db_connection_engine()

    if df.empty:
        print("Dataframe is empty. No data to insert.")
        return

    try:
        # Separate Info category
        df_string = df[df["Category"] == "Info"].copy()
        df = df[df["Category"] != "Info"].copy()

        df_string.reset_index(drop=True, inplace=True)

        if df_string.empty:
            print("No 'Info' category data found.")
            return

        # ---------------------------------------------------------
        # Remove duplicate BatchNo + Name combinations
        # ---------------------------------------------------------
        df_string = df_string.drop_duplicates(
            subset=["BatchNo", "Name"],
            keep="last"
        )

        # ---------------------------------------------------------
        # Pivot Info data safely
        # ---------------------------------------------------------
        df_pivot_1 = df_string.pivot_table(
            index='BatchNo',
            columns='Name',
            values='Value',
            aggfunc='first'
        )

        # Add Timestamp
        timestamp_df = (
            df_string
            .drop_duplicates(subset='BatchNo')
            .set_index('BatchNo')['Timestamp']
        )

        df_pivot_1['TimeStamp'] = timestamp_df

        df_pivot_1.reset_index(inplace=True)

        # ---------------------------------------------------------
        # Calculate Total Batch Weight
        # ---------------------------------------------------------
        if "ActualWeight" in df["Name"].values:

            df_weight = (
                df[df["Name"] == "ActualWeight"]
                .groupby("BatchNo")["Value"]
                .sum()
                .reset_index()
            )

            df_weight.rename(
                columns={"Value": "Total Batch Weight"},
                inplace=True
            )

            df_weight["Total Batch Weight"] = (
                df_weight["Total Batch Weight"]
                .round(2)
            )

            df_pivot_1 = df_pivot_1.merge(
                df_weight,
                on="BatchNo",
                how="left"
            )

        else:
            df_pivot_1["Total Batch Weight"] = 0

        # ---------------------------------------------------------
        # Add DailyBatchNo if available
        # ---------------------------------------------------------
        if "DailyBatchNo" in df.columns:

            df_daily = (
                df[['BatchNo', 'DailyBatchNo']]
                .drop_duplicates(subset=['BatchNo'])
            )

            df_pivot_1 = df_pivot_1.merge(
                df_daily,
                on='BatchNo',
                how='left'
            )

        # ---------------------------------------------------------
        # Check Postgres table columns
        # (was: PRAGMA table_info(batches) - SQLite only.
        # Postgres equivalent is information_schema.columns.)
        # ---------------------------------------------------------
        db_cols = pd.read_sql_query(
            """
            SELECT column_name AS name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'batches'
            """,
            engineConRead
        )['name'].tolist()

        print("Postgres columns:", db_cols)

        # Keep only columns that exist in DB
        insert_cols = [
            col for col in df_pivot_1.columns
            if col in db_cols
        ]

        df_insert = df_pivot_1[insert_cols]

        print("Columns to insert:")
        print(df_insert.columns.tolist())

        print(df_insert.head())

        # ---------------------------------------------------------
        # Insert into Postgres
        # ---------------------------------------------------------
        df_insert.to_sql(
            "batches",
            con=engineConWrite,
            if_exists="append",
            index=False,
            method='multi'
        )

        print(" Batch metadata inserted successfully.")

    except Exception as e:
        print(f" Error during batch insertion: {e}")

    finally:
        print("Batch insertion completed.")


# === Material Extraction Update ===
def insertMaterialExtraction(dfPlcdb, engineConRead, cursorWrite, conn):
    try:
        print("Starting Material Extraction...")

        # ---------------------------------------------------------
        # Prepare Material Index
        # ---------------------------------------------------------
        dfPlcdb = dfPlcdb.copy()

        # Get MaterialName values
        dfPlcdb['MaterialIndex'] = dfPlcdb.loc[
            dfPlcdb['Name'] == 'MaterialName',
            'Value'
        ]

        # Forward/Backward fill within each category
        dfPlcdb['MaterialIndex'] = (
            dfPlcdb.groupby('Category')['MaterialIndex']
            .transform(lambda x: x.ffill().bfill())
            .infer_objects(copy=False)
        )

        # Convert to string
        dfPlcdb['MaterialIndex'] = (
            dfPlcdb['MaterialIndex']
            .astype(str)
            .str.strip()
        )

        # Remove invalid PLC values
        invalid_values = [
            'nan',
            'None',
            '',
            '0.0',
            '-4.253529586511731e+37',
            '-4.253530e+37'
        ]

        dfPlcdb = dfPlcdb[
            ~dfPlcdb['MaterialIndex'].isin(invalid_values)
        ]

        # ---------------------------------------------------------
        # Filter Required Rows
        # ---------------------------------------------------------
        df_filtered = dfPlcdb[
            dfPlcdb['Name'].isin(['ActualWeight', 'SetWeight'])
        ].copy()

        if df_filtered.empty:
            print("No material data found.")
            return

        # ---------------------------------------------------------
        # Find duplicates
        # ---------------------------------------------------------
        duplicates = df_filtered[
            df_filtered.duplicated(
                subset=['MaterialIndex', 'Name'],
                keep=False
            )
        ]

        if not duplicates.empty:
            print("Duplicate rows found:")
            print(
                duplicates[
                    ['Category', 'MaterialIndex', 'Name', 'Value']
                ]
            )

        # Keep last duplicate
        df_filtered = df_filtered.drop_duplicates(
            subset=['MaterialIndex', 'Name'],
            keep='last'
        )

        # ---------------------------------------------------------
        # Pivot safely
        # ---------------------------------------------------------
        df_pivot = (
            df_filtered.pivot_table(
                index='MaterialIndex',
                columns='Name',
                values='Value',
                aggfunc='first'
            )
            .reset_index()
        )

        df_pivot.rename(
            columns={'MaterialIndex': 'MaterialName'},
            inplace=True
        )

        # ---------------------------------------------------------
        # Convert weights
        # ---------------------------------------------------------
        if 'ActualWeight' in df_pivot.columns:
            df_pivot['ActualWeight'] = (
                pd.to_numeric(
                    df_pivot['ActualWeight'],
                    errors='coerce'
                )
                .fillna(0)
                .div(1000)
                .round(2)
            )
        else:
            df_pivot['ActualWeight'] = 0

        # ---------------------------------------------------------
        # Read existing Postgres data
        # ---------------------------------------------------------
        existing_data = pd.read_sql(
            '''
            SELECT
                "SiloNo",
                "MaterialName",
                "TotalExtracted"
            FROM "MaterialData"
            ''',
            con=engineConRead
        )

        existing_data['MaterialName'] = (
            existing_data['MaterialName']
            .astype(str)
            .str.strip()
        )

        df_pivot['MaterialName'] = (
            df_pivot['MaterialName']
            .astype(str)
            .str.strip()
        )

        print("\nPLC Materials:")
        print(df_pivot['MaterialName'].tolist())

        print("\nDB Materials:")
        print(existing_data['MaterialName'].tolist())

        # ---------------------------------------------------------
        # Merge
        # ---------------------------------------------------------
        df_merged = pd.merge(
            df_pivot,
            existing_data,
            on='MaterialName',
            how='inner'
        )

        if df_merged.empty:
            print("No matching materials found.")
            return

        df_merged['TotalExtracted'] = pd.to_numeric(
            df_merged['TotalExtracted'],
            errors='coerce'
        ).fillna(0)

        df_merged['TotalWeight'] = (
            df_merged['ActualWeight'] +
            df_merged['TotalExtracted']
        )

        print("\nMerged Data:")
        print(df_merged)

        # ---------------------------------------------------------
        # Update Postgres (was: "?" placeholders - now "%s")
        # ---------------------------------------------------------
        update_query = """
            UPDATE "MaterialData"
            SET "TotalExtracted" = %s
            WHERE "MaterialName" = %s
        """

        for _, row in df_merged.iterrows():
            cursorWrite.execute(
                update_query,
                (
                    float(row['TotalWeight']),
                    row['MaterialName']
                )
            )

        conn.commit()

        print(
            " TotalWeight values successfully updated "
            "in MaterialData."
        )

    except Exception as e:
        import traceback
        print(" Error occurred:", e)
        traceback.print_exc()


def data_batch(conn, hours, from_time, to_time, engineConRead):

    try:
        if hours == "Custom":
            print("Time:", from_time, to_time)

            try:
                from_time_dt = datetime.fromisoformat(from_time)
                to_time_dt = datetime.fromisoformat(to_time)
            except Exception:
                print(" Invalid datetime format, received:", from_time, to_time)
                return None

            query = """
                SELECT DISTINCT * FROM "Batches"
                WHERE "TimeStamp" BETWEEN %s AND %s
                ORDER BY "TimeStamp" ASC;
            """
            params = (from_time_dt, to_time_dt)

        elif hours in ["1 Hr", "4 Hr", "8 Hr", "12 Hr", "24 Hr"]:
            hours_mapping = {"1 Hr": 1, "4 Hr": 4, "8 Hr": 8, "12 Hr": 12, "24 Hr": 24}
            from_time_dt = datetime.now() - timedelta(hours=hours_mapping[hours])

            query = """
                SELECT DISTINCT * FROM "Batches"
                WHERE "TimeStamp" >= %s
                ORDER BY "TimeStamp" ASC;
            """
            params = (from_time_dt,)
        else:
            print(" Invalid hours option:", hours)
            return None

        

        df = pd.read_sql_query(query, con=engineConRead, params=params)

        if df.empty:
            print(" No data returned for given filters.")
            return None

        df = df.drop_duplicates(subset=["BatchNo"], keep="first")
        print(f" Retrieved {len(df)} records.")
        return df

    except Exception as e:
        print(f" Error in data_batch: {e}")
        return None


def get_silo_pivot(df: pd.DataFrame, silo: str) -> pd.DataFrame:
 
    # Step 1: Filter by silo and remove unwanted rows
    df_filtered = df[df["Category"] == silo].copy()
    df_filtered = df_filtered[~((df_filtered["Category"] == "Info") | (df_filtered["DataType"] == "STRING"))]

    # Step 2: Ensure Value is numeric
    df_filtered["Value"] = pd.to_numeric(df_filtered["Value"], errors="coerce")

    # Step 2b: Convert TimeStamp to datetime and truncate to minutes
    df_filtered["TimeStamp"] = pd.to_datetime(df_filtered["TimeStamp"], errors="coerce")
    df_filtered["TimeStamp"] = df_filtered["TimeStamp"].dt.floor('min')

    # Step 3: Pivot to wide format with minute-level TimeStamp
    df_pivot = (
        df_filtered.pivot_table(
            index=["Category", "TimeStamp"],
            columns="Name",
            values="Value",
            aggfunc="first"
        )
        .reset_index()
    )

    # Step 4: Keep only required columns safely
    required_cols = ["Category", "TimeStamp", "SetWeight", "ActualWeight", "FineWeight"]
    available_cols = [col for col in required_cols if col in df_pivot.columns]
    df_pivot = df_pivot[available_cols]

    # Step 5: Convert weights to numeric
    for col in ["SetWeight", "ActualWeight"]:
        if col in df_pivot.columns:
            df_pivot[col] = pd.to_numeric(df_pivot[col], errors="coerce").fillna(0)

    # Step 6: Row-wise error calculations
    error_kg_list = []
    error_perc_list = []
    for idx, row in df_pivot.iterrows():
        set_wt = row["SetWeight"]
        actual_wt = row["ActualWeight"]

        error_kg = actual_wt - set_wt
        error_perc = (error_kg / set_wt * 100) if set_wt != 0 else 0.0

        error_kg_list.append(error_kg)
        error_perc_list.append(error_perc)

    df_pivot["Error_Kg"] = error_kg_list
    df_pivot["Error_%"] = error_perc_list

    df_pivot["Error_Kg"] = df_pivot["Error_Kg"].round(2)
    df_pivot["Error_%"] = df_pivot["Error_%"].round(2)

    return df_pivot


def show_data(conn, hours, from_time, to_time, engineConRead):
    
    try:
        if hours == "Custom":
            print("Time:", from_time, to_time)

            from_time_dt = datetime.fromisoformat(from_time)
            to_time_dt = datetime.fromisoformat(to_time)

            date_diff = to_time_dt - from_time_dt
            print("Date Difference:", date_diff.days)

            query = """
                SELECT * FROM plc_data
                WHERE "TimeStamp" BETWEEN %s AND %s
                ORDER BY "TimeStamp" ASC;
            """
            params = (from_time_dt, to_time_dt)

            
            df = pd.read_sql_query(query, engineConRead, params=params)

        elif hours in ["1 Hr", "4 Hr", "8 Hr", "12 Hr", "24 Hr"]:
            hours_mapping = {
                "1 Hr": 1,
                "4 Hr": 4,
                "8 Hr": 8,
                "12 Hr": 12,
                "24 Hr": 24
            }
            hours_ago = hours_mapping[hours]
            from_time_dt = datetime.now() - timedelta(hours=hours_ago)

            query = """
                SELECT *
                FROM plc_data
                WHERE "TimeStamp" >= %s
                ORDER BY "TimeStamp" ASC;
            """
            params = (from_time_dt,)

            
            df = pd.read_sql_query(query, engineConRead, params=params)

        else:
            print("Select a valid time range")
            return None

        return df

    except Exception as e:
        print(f"An error occurred: {e}")
        return None


def process_batch_data(df: pd.DataFrame) -> pd.DataFrame:
   
    df1 = df[~((df['Category'] == "Info") | (df['DataType'] == "STRING"))].copy()

    df1["Value_num"] = pd.to_numeric(df1["Value"], errors="coerce")
    df1.drop("Value", axis=1, inplace=True)

    df_pivot = df1.pivot_table(
        index=["BatchNo", "Category"],
        columns=["Name"],
        values="Value_num"
    )

    df_pivot["Error_Kg"] = df_pivot["ActualWeight"] - df_pivot["SetWeight"]
    df_pivot["Error_%"] = (df_pivot["Error_Kg"] / df_pivot["SetWeight"]) * 100

    Q1 = df_pivot["Error_%"].quantile(0.25)
    Q3 = df_pivot["Error_%"].quantile(0.75)
    IQR = Q3 - Q1
    df_clean = df_pivot[
        (df_pivot["Error_%"] >= (Q1 - 1.5 * IQR)) &
        (df_pivot["Error_%"] <= (Q3 + 1.5 * IQR))
    ]

    df_group = (
        df_clean.groupby("Category")
        .agg({
            "ActualWeight": "sum",
            "SetWeight": "sum",
            "Error_Kg": "sum",
            "Error_%": "mean"
        })
        .sort_values("Error_%", ascending=False)
        .reset_index()
    )
    df_group["Error_Kg"] = df_group["Error_Kg"].round(2)
    df_group["Error_%"] = df_group["Error_%"].round(2)

    return df_group