import sqlite3
from sqlalchemy import create_engine
import pandas as pd
from config.config import DB_CONFIG
from datetime import datetime, time, timedelta

# === Direct SQLite Connection (for raw cursor use) ===
def get_db_connection():
    try:
        conn = sqlite3.connect(DB_CONFIG)
        conn.row_factory = sqlite3.Row  # Dict-like cursor
        cursorRead = conn.cursor()
        cursorWrite = conn.cursor()
        return conn, cursorRead, cursorWrite
    except Exception as e:
        print(f"Failed to connect to SQLite: {e}")
        return None


# === SQLAlchemy Engine for pandas.to_sql and read_sql ===
# === SQLAlchemy Engine for pandas.to_sql and read_sql ===
def get_db_connection_engine():
    try:
        # SQLite connection URL
        db_url = f"sqlite:///{DB_CONFIG}"

        # Create SQLAlchemy engine
        engine = create_engine(db_url, echo=False)

        # Separate read and write connections
        engineConRead = engine.connect()
        engineConWrite = engine.connect()

        print("✅ SQLite SQLAlchemy engine created successfully.")
        return engine, engineConRead, engineConWrite

    except Exception as e:
        print(f"❌ Failed to create SQLAlchemy engine: {e}")
        return None, None, None

# === Example Function to Read Users Table ===
def dfUser():
    conn, cursorRead, cursorWrite = get_db_connection()
    engine, engineConRead, engineConWrite = get_db_connection_engine()
    query = "SELECT id, username, role, is_active, last_login FROM users WHERE role != 'superadmin';"
    df = pd.read_sql_query(query, conn)
    df.columns = ['Id', 'Username', 'Role', 'Is_Active', 'LastLogin']
    df = df.sort_values(by='Id', ascending=True).reset_index(drop=True)

    # Add UI numbering (starting from 1)
    df.insert(0, 'UID', df.index + 1)

    return df


# === Insert Batch Function ===
def insertBatch(df):
    print("Starting batch insertion...")

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
        # Check SQLite table columns
        # ---------------------------------------------------------
        db_cols = pd.read_sql_query(
            'PRAGMA table_info(batches);',
            engineConRead
        )['name'].tolist()

        print("SQLite columns:", db_cols)

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
        # Insert into SQLite
        # ---------------------------------------------------------
        df_insert.to_sql(
            "batches",
            con=engineConWrite,
            if_exists="append",
            index=False,
            method='multi'
        )

        print("✅ Batch metadata inserted successfully.")

    except Exception as e:
        print(f"❌ Error during batch insertion: {e}")

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
        # Read existing SQLite data
        # ---------------------------------------------------------
        existing_data = pd.read_sql(
            '''
            SELECT
                SiloNo,
                MaterialName,
                TotalExtracted
            FROM MaterialData
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
        # Update SQLite
        # ---------------------------------------------------------
        update_query = """
            UPDATE MaterialData
            SET TotalExtracted = ?
            WHERE MaterialName = ?
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
            "✅ TotalWeight values successfully updated "
            "in MaterialData."
        )

    except Exception as e:
        import traceback
        print("❌ Error occurred:", e)
        traceback.print_exc()
        
def data_batch(conn, hours, from_time, to_time, engineConRead):
    try:
        if hours == "Custom":
            print("Time:", from_time, to_time)

            try:
                from_time_dt = datetime.fromisoformat(from_time)
                to_time_dt = datetime.fromisoformat(to_time)
            except Exception:
                print("⚠️ Invalid datetime format, received:", from_time, to_time)
                return None

            from_time_sql = from_time_dt.strftime('%Y-%m-%d %H:%M:%S')
            to_time_sql = to_time_dt.strftime('%Y-%m-%d %H:%M:%S')

            query = f"""
                SELECT DISTINCT * FROM Batches
                WHERE TimeStamp BETWEEN '{from_time_sql}' AND '{to_time_sql}'
                ORDER BY TimeStamp ASC;
            """

        elif hours in ["1 Hr", "4 Hr", "8 Hr", "12 Hr", "24 Hr"]:
            hours_mapping = {"1 Hr": 1, "4 Hr": 4, "8 Hr": 8, "12 Hr": 12, "24 Hr": 24}
            from_time_dt = datetime.now() - timedelta(hours=hours_mapping[hours])
            from_time_sql = from_time_dt.strftime('%Y-%m-%d %H:%M:%S')

            query = f"""
                SELECT DISTINCT * FROM Batches
                WHERE TimeStamp >= '{from_time_sql}'
                ORDER BY TimeStamp ASC;
            """
        else:
            print("⚠️ Invalid hours option:", hours)
            return None

        print("Executing query:\n", query)

        # ✅ Fix: pass engine or connection directly — not a transaction object
        df = pd.read_sql_query(query, con=engineConRead)

        if df.empty:
            print("ℹ️ No data returned for given filters.")
            return None

        df = df.drop_duplicates(subset=["BatchNo"], keep="first")
        print(f"✅ Retrieved {len(df)} records.")
        return df

    except Exception as e:
        print(f"❌ Error in data_batch: {e}")
        return None
    
def get_silo_pivot(df: pd.DataFrame, silo: str) -> pd.DataFrame:
    """
    Filter and pivot silo data per timestamp (minute-level) for a given silo.

    Args:
        df (pd.DataFrame): Raw DataFrame with columns [Category, TimeStamp, Name, Value, DataType].
        silo (str): The silo name (e.g., "Silo-1").

    Returns:
        pd.DataFrame: Pivoted DataFrame with required columns + row-wise error calculations.
    """
    # Step 1: Filter by silo and remove unwanted rows
    df_filtered = df[df["Category"] == silo].copy()
    df_filtered = df_filtered[~((df_filtered["Category"] == "Info") | (df_filtered["DataType"] == "STRING"))]

    # Step 2: Ensure Value is numeric
    df_filtered["Value"] = pd.to_numeric(df_filtered["Value"], errors="coerce")

    # Step 2b: Convert TimeStamp to datetime and truncate to minutes
    df_filtered["TimeStamp"] = pd.to_datetime(df_filtered["TimeStamp"], errors="coerce")
    df_filtered["TimeStamp"] = df_filtered["TimeStamp"].dt.floor('min')  # use 'min' instead of deprecated 'T'

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

            # Calculate the difference
            date_diff = to_time_dt - from_time_dt
            print("Date Difference:", date_diff.days)    
            
            if date_diff.days >= 30:
                # Query database for data between specified timestamps from both tables
                query = f"""
                SELECT * FROM plc_data
                WHERE TimeStamp BETWEEN '{from_time.replace("T", " ")}' AND '{to_time.replace("T", " ")}'
                
                UNION ALL
                
                SELECT * FROM plc_data
                WHERE TimeStamp BETWEEN '{from_time.replace("T", " ")}' AND '{to_time.replace("T", " ")}'
                ORDER BY TimeStamp ASC;
                """
            else:
                # Query database for data between specified timestamps   
                query = f"""
                SELECT * FROM plc_data
                WHERE TimeStamp BETWEEN '{from_time.replace("T", " ")}' AND '{to_time.replace("T", " ")}'
                ORDER BY TimeStamp ASC;
                """
            print("Executing query:", query)
            df = pd.read_sql_query(query, engineConRead)

        elif hours in ["1 Hr", "4 Hr", "8 Hr", "12 Hr", "24 Hr"]:
            # Determine the hour range for predefined selections
            hours_mapping = {
                "1 Hr": 1,
                "4 Hr": 4,
                "8 Hr": 8,
                "12 Hr": 12,
                "24 Hr": 24
            }
            hours_ago = hours_mapping[hours]

            # Calculate the time range for the query
            from_time_dt = datetime.now() - timedelta(hours=hours_ago)
            from_time = from_time_dt.strftime('%Y-%m-%d %H:%M:%S')

            # Construct the SQL query based on the selected hours range
            query = f"""
                SELECT *
                FROM plc_data
                WHERE TimeStamp >= '{from_time}'
                ORDER BY TimeStamp ASC;
            """
            print("Executing query:", query)
            df = pd.read_sql_query(query, engineConRead)

        else:
            print("Select a valid time range")
            return None

        # Return the DataFrame for further processing
        return df

    except Exception as e:
        print(f"An error occurred: {e}")
        return None



def process_batch_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Cleans and processes batch data:
    1. Drops rows with Category == 'Info' or DataType == 'STRING'
    2. Converts Value to numeric
    3. Creates pivot with BatchNo + Category
    4. Calculates Error_Kg and Error_%
    5. Removes outliers using IQR
    6. Groups by Category and aggregates
    
    Returns:
        pd.DataFrame: Grouped and aggregated results
    """
    # Step 1: Drop unwanted rows
    df1 = df[~((df['Category'] == "Info") | (df['DataType'] == "STRING"))].copy()
    
    # Step 2: Convert Value column
    df1["Value_num"] = pd.to_numeric(df1["Value"], errors="coerce")
    df1.drop("Value", axis=1, inplace=True)
    
    # Step 3: Pivot
    df_pivot = df1.pivot_table(
        index=["BatchNo", "Category"], 
        columns=["Name"], 
        values="Value_num"
    )
    
    # Step 4: Error calculations
    df_pivot["Error_Kg"] = df_pivot["ActualWeight"] - df_pivot["SetWeight"]
    df_pivot["Error_%"] = (df_pivot["Error_Kg"] / df_pivot["SetWeight"]) * 100
    
    # Step 5: IQR outlier removal
    Q1 = df_pivot["Error_%"].quantile(0.25)
    Q3 = df_pivot["Error_%"].quantile(0.75)
    IQR = Q3 - Q1
    df_clean = df_pivot[
        (df_pivot["Error_%"] >= (Q1 - 1.5 * IQR)) &
        (df_pivot["Error_%"] <= (Q3 + 1.5 * IQR))
    ]
    
    # Step 6: Group and aggregate
    df_group = (
        df_clean.groupby("Category")
        .agg({
            "ActualWeight": "sum",
            "SetWeight": "sum",
            "Error_Kg": "sum",
            "Error_%": "mean"
        })
        .sort_values("Error_%", ascending=False)
        .reset_index()   # 👈 this ensures Category is kept as a column
    )
    # Round error columns
    df_group["Error_Kg"] = df_group["Error_Kg"].round(2)
    df_group["Error_%"] = df_group["Error_%"].round(2)
    
    return df_group
