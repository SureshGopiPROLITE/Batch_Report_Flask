from sqlalchemy import create_engine
import shutil
import os
import subprocess
import psycopg2
import pandas as pd
from datetime import datetime, timedelta
# from database import postgres

import os

# ---------------------------------------------------------------------------
# Connection settings
# ---------------------------------------------------------------------------
from config.config import DB_CONFIG

DB_HOST = DB_CONFIG["host"]
DB_PORT = DB_CONFIG["port"]
DB_NAME = DB_CONFIG["dbname"]
DB_USER = DB_CONFIG["user"]
DB_PASSWORD = DB_CONFIG["password"]

ENGINE_URL = (
    f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

# ---------------------------------------------------------------------------
# FIX: create ONE engine (and its own small pool) for the whole process,
# ---------------------------------------------------------------------------
engine = create_engine(
    ENGINE_URL,
    pool_size=5,
    max_overflow=2,
    pool_pre_ping=True,
    pool_recycle=1800,
)


def postgres():  # Keep same name so the rest of the project works unchanged

    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )
    cursorRead = conn.cursor()
    cursorWrite = conn.cursor()
    engineConRead = engine.connect()
    engineConWrite = engine.connect()
    return cursorRead, cursorWrite, engineConRead, engineConWrite, conn


def connect():
    """Single psycopg2 connection, for work that must commit as one transaction."""
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def ensure_indexes():
    """Idempotent startup schema upgrades. plc_data grows by ~50 rows per batch;
    without the indexes every batch-number lookup and report is a full scan.
    "Seq" is the user-arranged step order of a recipe's rows (download order)."""
    statements = [
        'CREATE INDEX IF NOT EXISTS ix_plc_data_timestamp ON plc_data ("TimeStamp")',
        'CREATE INDEX IF NOT EXISTS ix_plc_data_batchno ON plc_data ("BatchNo")',
        'CREATE INDEX IF NOT EXISTS ix_batches_timestamp ON "Batches" ("TimeStamp")',
        'ALTER TABLE "recipeData" ADD COLUMN IF NOT EXISTS "Seq" integer',
        # Recipe page: recipes.category is the set a recipe is listed under
        # ("Recipe Set 1"), for display only. It used to be a copy of the name
        # (never read) - clear those so each recipe is not its own set.
        "UPDATE recipes SET category = NULL WHERE category = name",
        # Steps of recipes deleted before delete removed them too: nothing
        # shows them, and a re-import of that recipe would list them twice
        """DELETE FROM "recipeData" d
           WHERE NOT EXISTS (SELECT 1 FROM recipes r WHERE r.name = d."Category")""",
        # A pre-release build kept the set in "SetName": move it to category
        """DO $$ BEGIN
               IF EXISTS (SELECT 1 FROM information_schema.columns
                          WHERE table_name = 'recipes' AND column_name = 'SetName') THEN
                   UPDATE recipes SET category = "SetName" WHERE "SetName" IS NOT NULL;
                   ALTER TABLE recipes DROP COLUMN "SetName";
               END IF;
           END $$""",
        # recipes.id had no default: recipes added in the app got NULL
        'CREATE SEQUENCE IF NOT EXISTS recipes_id_seq',
        """SELECT setval('recipes_id_seq', GREATEST((SELECT COALESCE(MAX(id), 0) FROM recipes), 1))""",
        "UPDATE recipes SET id = nextval('recipes_id_seq') WHERE id IS NULL",
        "ALTER TABLE recipes ALTER COLUMN id SET DEFAULT nextval('recipes_id_seq')",
        # Shift of each batch (Settings shift times + batch start time)
        'ALTER TABLE "Batches" ADD COLUMN IF NOT EXISTS "Shift" text',
        # users.user_access was never used for permissions (the role is)
        'ALTER TABLE users DROP COLUMN IF EXISTS user_access',
        # users.id had no default, so users added in User Management got NULL
        # and could not be edited / (de)activated. Number them like recipeData.
        'CREATE SEQUENCE IF NOT EXISTS users_id_seq',
        """SELECT setval('users_id_seq', GREATEST((SELECT COALESCE(MAX(id), 0) FROM users), 1))""",
        "UPDATE users SET id = nextval('users_id_seq') WHERE id IS NULL",
        "ALTER TABLE users ALTER COLUMN id SET DEFAULT nextval('users_id_seq')",
        # Silo Materials page: when a silo row was last changed (assign / edit /
        # reset weight); "OperatorName" holds who did it
        'ALTER TABLE "MaterialData" ADD COLUMN IF NOT EXISTS "UpdatedAt" timestamp',
        # A pre-release build kept "who" twice (OperatorName + UpdatedBy):
        # keep the newer name in OperatorName and drop the duplicate column
        """DO $$ BEGIN
               IF EXISTS (SELECT 1 FROM information_schema.columns
                          WHERE table_name = 'MaterialData' AND column_name = 'UpdatedBy') THEN
                   UPDATE "MaterialData" SET "OperatorName" = "UpdatedBy"
                   WHERE "UpdatedBy" IS NOT NULL;
                   ALTER TABLE "MaterialData" DROP COLUMN "UpdatedBy";
               END IF;
           END $$""",
        # Material Catalogue page: the material master list the Silo Materials page
        # suggests from. Names are unique ignoring case / outer spaces.
        """CREATE TABLE IF NOT EXISTS "MaterialMaster" (
               "Id" serial PRIMARY KEY,
               "MaterialName" text NOT NULL,
               "MaterialCode" text,
               "UpdatedAt" timestamp,
               "UpdatedBy" text)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_materialmaster_name
           ON "MaterialMaster" (LOWER(TRIM("MaterialName")))""",
        # First start: fill the list with the materials already in the silos
        """INSERT INTO "MaterialMaster" ("MaterialName", "MaterialCode", "UpdatedAt", "UpdatedBy")
           SELECT DISTINCT ON (LOWER(TRIM("MaterialName"))) TRIM("MaterialName"),
                  NULLIF(TRIM("MaterialCode"), ''), now(), 'system'
           FROM "MaterialData"
           WHERE NULLIF(TRIM("MaterialName"), '') IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM "MaterialMaster")
           ORDER BY LOWER(TRIM("MaterialName"))""",
        # Material still falling after the feeder stops (kg); written to the
        # PLC tag Recipe_Data[silo].InflightWeight
        'ALTER TABLE "recipeData" ADD COLUMN IF NOT EXISTS "InflightWeight" double precision DEFAULT 0',
        # "Index" is the row id that edit / delete / reorder / download use.
        # It never had a default, so every row added through the app got NULL
        # and all of a recipe's rows looked like one. Give it a sequence and
        # fill in the missing ones (existing ids are kept).
        'CREATE SEQUENCE IF NOT EXISTS "recipeData_Index_seq"',
        """SELECT setval('"recipeData_Index_seq"',
                         GREATEST((SELECT COALESCE(MAX("Index"), 0) FROM "recipeData"), 1))""",
        """UPDATE "recipeData" SET "Index" = nextval('"recipeData_Index_seq"')
           WHERE "Index" IS NULL""",
        """UPDATE "recipeData" r SET "Index" = nextval('"recipeData_Index_seq"')
           FROM (SELECT ctid, ROW_NUMBER() OVER (PARTITION BY "Index" ORDER BY ctid) AS n
                 FROM "recipeData") d
           WHERE r.ctid = d.ctid AND d.n > 1""",
        """ALTER TABLE "recipeData" ALTER COLUMN "Index"
           SET DEFAULT nextval('"recipeData_Index_seq"')""",
    ]
    from config.version import SOFTWARE_VERSION

    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            for sql in statements:
                cur.execute(sql)

            # Keep Info_db's Software_version equal to the installed software
            cur.execute('UPDATE "Info_db" SET "Info" = %s WHERE "Particulars" = %s',
                        (SOFTWARE_VERSION, "Software_version"))
            if cur.rowcount == 0:
                cur.execute('INSERT INTO "Info_db" ("Id", "Particulars", "Info") '
                            'SELECT COALESCE(MAX("Id"), 0) + 1, %s, %s FROM "Info_db"',
                            ("Software_version", SOFTWARE_VERSION))
    finally:
        conn.close()


def close_postgres(cursorRead, cursorWrite, engineConRead, engineConWrite, conn):
 
    for obj in (cursorRead, cursorWrite, engineConRead, engineConWrite, conn):
        if obj is None:
            continue
        try:
            obj.close()
        except Exception as e:
            print(f"Warning: error closing {obj!r}: {e}")


def calculate_silo_diff(dfPlcdb: pd.DataFrame) -> pd.DataFrame:

    results = [
        {
            "Timestamp": group["Timestamp"].iloc[0],
            "Name": name,
            "data_type": "REAL",
            "Value": abs(group.loc[group["Name"] == "ActualWeight", "Value"].values[0] -
                         group.loc[group["Name"] == "SetWeight", "Value"].values[0])
                     if name == "DiffKg" else
                     (abs(group.loc[group["Name"] == "ActualWeight", "Value"].values[0] -
                          group.loc[group["Name"] == "SetWeight", "Value"].values[0]) /
                      group.loc[group["Name"] == "SetWeight", "Value"].values[0] * 100
                      if group.loc[group["Name"] == "SetWeight", "Value"].values[0] != 0 else None),
            "Category": silo,
            "BatchNo": group["BatchNo"].iloc[0],
            "DailyBatchNo": group["DailyBatchNo"].iloc[0]
        }
        for silo, group in dfPlcdb.groupby("Category")
        for name in ["DiffKg", "DiffPerc"]
        if not group[group["Name"].isin(["SetWeight", "ActualWeight"])].empty
    ]

    return pd.concat([dfPlcdb, pd.DataFrame(results)], ignore_index=True)


def backup_database():
    try:
        from tkinter import Tk, filedialog  # desktop-only dialog

        # Hide the root window
        root = Tk()
        root.withdraw()

        # Ask the user to select the destination for the backup
        backup_path = filedialog.asksaveasfilename(
            title="Select Backup Destination",
            defaultextension=".dump",
            filetypes=[("Postgres Dump Files", "*.dump")]
        )
        if not backup_path:
            print("No backup destination selected.")
            return

        backup_dir = os.path.dirname(backup_path)
        if backup_dir and not os.path.exists(backup_dir):
            os.makedirs(backup_dir)

        env = os.environ.copy()
        env["PGPASSWORD"] = DB_PASSWORD

        cmd = [
            "pg_dump",
            "-h", DB_HOST,
            "-p", DB_PORT,
            "-U", DB_USER,
            "-F", "c",
            "-f", backup_path,
            DB_NAME,
        ]

        result = subprocess.run(cmd, env=env, capture_output=True, text=True)

        if result.returncode != 0:
            print(f"pg_dump failed: {result.stderr}")
            return

        print(f"Backup of database '{DB_NAME}' completed successfully at '{backup_path}'.")

    except FileNotFoundError:
        print("pg_dump was not found. Make sure the PostgreSQL client tools are installed and on PATH.")
    except Exception as e:
        print(f"Error occurred: {str(e)}")


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
                WHERE "TimeStamp" BETWEEN '{from_time.replace("T", " ")}' AND '{to_time.replace("T", " ")}'

                UNION ALL

                SELECT * FROM plc_data
                WHERE "TimeStamp" BETWEEN '{from_time.replace("T", " ")}' AND '{to_time.replace("T", " ")}'
                ORDER BY "TimeStamp" ASC;
                """
            else:
                # Query database for data between specified timestamps
                query = f"""
                SELECT * FROM plc_data
                WHERE "TimeStamp" BETWEEN '{from_time.replace("T", " ")}' AND '{to_time.replace("T", " ")}'
                ORDER BY "TimeStamp" ASC;
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
                WHERE "TimeStamp" >= '{from_time}'
                ORDER BY "TimeStamp" ASC;
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


def showBatch(conn, hours, from_time, to_time, engineConRead):
    try:
        if hours == "Custom":
            print("Time:", from_time, to_time)

            from_time_dt = datetime.fromisoformat(from_time)
            to_time_dt = datetime.fromisoformat(to_time)
            date_diff = to_time_dt - from_time_dt
            print("Date Difference:", date_diff.days)

            query = f"""
            SELECT DISTINCT * FROM "Batches"
            WHERE "TimeStamp" BETWEEN '{from_time.replace("T", " ")}' AND '{to_time.replace("T", " ")}'
            ORDER BY "TimeStamp" ASC;
            """

        elif hours in ["1 Hr", "4 Hr", "8 Hr", "12 Hr", "24 Hr"]:
            hours_mapping = {"1 Hr": 1, "4 Hr": 4, "8 Hr": 8, "12 Hr": 12, "24 Hr": 24}
            from_time_dt = datetime.now() - timedelta(hours=hours_mapping[hours])
            from_time = from_time_dt.strftime('%Y-%m-%d %H:%M:%S')

            query = f"""
            SELECT DISTINCT * FROM "Batches"
            WHERE "TimeStamp" >= '{from_time}'
            ORDER BY "TimeStamp" ASC;
            """

        else:
            print("Select a valid time range")
            return None

        print("Executing query:", query)
        df = pd.read_sql_query(query, engineConRead)

        # Drop duplicates if still present
        df = df.drop_duplicates(subset=["BatchNo"])  # Keep only unique BatchNo

        return df

    except Exception as e:
        print(f"An error occurred: {e}")
        return None


def insert_data_into_sqlite(cursor, conn, dfPlcExcel):

    try:
        # Drop the table if it exists
        drop_table_query = 'DROP TABLE IF EXISTS "Data"'
        cursor.execute(drop_table_query)
        print("Dropped existing table (if any).")

        # Determine Postgres data types
        postgres_types = {
            'int64': 'INTEGER',
            'float64': 'REAL',
            'bool': 'BOOLEAN',
            'object': 'TEXT'
        }

        # Dynamically generate CREATE TABLE query based on DataFrame columns
        columns = ', '.join([f'"{col}" {postgres_types[str(dfPlcExcel[col].dtype)]}' for col in dfPlcExcel.columns])
        create_table_query = f"""
            CREATE TABLE IF NOT EXISTS "Data" ({columns});
        """
        cursor.execute(create_table_query)
        print("Created new table based on DataFrame columns.")

        # Insert data into the table
        for index, row in dfPlcExcel.iterrows():
            placeholders = ', '.join(['%s' for _ in dfPlcExcel.columns])
            columns = ', '.join([f'"{c}"' for c in dfPlcExcel.columns])
            sql = f'INSERT INTO "Data" ({columns}) VALUES ({placeholders})'
            cursor.execute(sql, tuple(row))
        print("Inserted data into the new table.")

        # Commit changes
        conn.commit()
        print("Data committed to Postgres database.")

    except Exception as e:
        print(f"Error occurred: {str(e)}")


def dfPlc(conn, softwaretype):
    try:
        print("Inside the DF")
        print("Software Type:", softwaretype)
        select_query = 'SELECT * FROM "Data"'
        dfPlcdb = pd.read_sql_query(select_query, conn)
        print("Data Type:", dfPlcdb)
        if softwaretype == 0:
            print("Software 1")
            dfPlcdb = dfPlcdb.head(600)
        else:
            print("Software 2")
            softwaretype = int(softwaretype)
            dfPlcdb = dfPlcdb.head(softwaretype * 10)
        print(dfPlcdb)
        # Convert columns to numeric if they exist
        numeric_columns = ['db_number', 'start_offset', 'bit_offset']
        for col in numeric_columns:
            if col in dfPlcdb.columns:
                # Convert non-numeric values to NaN and then fill with a default value if needed
                dfPlcdb[col] = pd.to_numeric(dfPlcdb[col], errors='coerce')
                dfPlcdb[col].fillna(0, inplace=True)
        print(dfPlcdb)
        return dfPlcdb

    except Exception as e:
        print(f"An error occurred: {e}")
        return None


def insertBatch(df):

    # Establish database connection
    cursorRead, cursorWrite, engineConRead, engineConWriten, conn = postgres()

    # FIX: wrap everything in try/finally so the connections opened by
    # postgres() are always released, even if something below raises.
    try:
        # Separate Info category from other data
        df_string, df = df[df["Category"] == "Info"], df[df["Category"] != "Info"]
        df_string = df_string.reset_index()

        # Pivot df_string
        df_pivot_1 = df_string.pivot(index='BatchNo', columns='Name', values='Value')
        df_pivot_1['TimeStamp'] = df_string.drop_duplicates(subset='BatchNo').set_index('BatchNo')['Timestamp']
        df_pivot_1 = df_pivot_1.reset_index()

        # Reorder columns (ensure these column names exist in your dataframe)
        column_order = ["BatchNo", "TimeStamp", "Plant Name", "Recipe Name", "Start Date Time", "End Date Time"]
        df_pivot_1 = df_pivot_1[column_order]

        # Extract Total Batch Weight from df
        df_weight = df[df["Name"] == "ActualWeight"]
        df_weight = df_weight.groupby("BatchNo")["Value"].sum().reset_index()

        # Round to 2 decimal places
        df_weight["Value"] = pd.to_numeric(
            df_weight["Value"],
            errors="coerce"
        )

        df_weight["Value"] = df_weight["Value"].round(2)

        df_weight.rename(columns={"Value": "Total Batch Weight"}, inplace=True)

        # Merge with df_pivot_1
        df_pivot_1 = df_pivot_1.merge(df_weight, on="BatchNo", how="left")

        # Insert df_pivot_1 into the Batches table
        # (to_sql via SQLAlchemy quotes mixed-case identifiers automatically)
        df_pivot_1.to_sql("Batches", con=engineConWriten, if_exists="append", index=False)
    finally:
        close_postgres(cursorRead, cursorWrite, engineConRead, engineConWriten, conn)


# recipe tag inc
def insert_data_into_sqlite_rec(cursor, conn, dfPlcExcel):
    try:
        # Drop the table if it exists
        drop_table_query = 'DROP TABLE IF EXISTS "RecipeTagName"'
        cursor.execute(drop_table_query)
        print("Dropped existing table (if any).")

        # Determine Postgres data types
        postgres_types = {
            'int64': 'INTEGER',
            'float64': 'REAL',
            'bool': 'BOOLEAN',
            'object': 'TEXT'
        }

        # Dynamically generate CREATE TABLE query based on DataFrame columns
        columns = ', '.join([f'"{col}" {postgres_types[str(dfPlcExcel[col].dtype)]}' for col in dfPlcExcel.columns])
        create_table_query = f"""
            CREATE TABLE IF NOT EXISTS "RecipeTagName" ({columns});
        """
        cursor.execute(create_table_query)
        print("Created new table based on DataFrame columns.")

        # Insert data into the table
        for index, row in dfPlcExcel.iterrows():
            placeholders = ', '.join(['%s' for _ in dfPlcExcel.columns])
            columns = ', '.join([f'"{c}"' for c in dfPlcExcel.columns])
            sql = f'INSERT INTO "RecipeTagName" ({columns}) VALUES ({placeholders})'
            cursor.execute(sql, tuple(row))
        print("Inserted data into the new table.")

        # Commit changes
        conn.commit()
        print("Data committed to Postgres database.")

    except Exception as e:
        print(f"Error occurred: {str(e)}")


def insertMaterialExtraction(dfPlcdb, engineConRead, cursorWrite, conn):
    try:

        # Extract MaterialName values and forward-fill
        dfPlcdb['MaterialIndex'] = dfPlcdb.loc[dfPlcdb['Name'] == 'MaterialName', 'Value']
        dfPlcdb['MaterialIndex'] = dfPlcdb.groupby('Category')['MaterialIndex'].transform(lambda x: x.ffill().bfill())
        dfPlcdb = dfPlcdb.infer_objects(copy=False)  # Fix FutureWarning

        # Filter relevant rows and pivot
        df_filtered = dfPlcdb[dfPlcdb['Name'].isin(['ActualWeight', 'SetWeight'])].reset_index(drop=True)
        df_pivot = df_filtered.pivot(index='MaterialIndex', columns='Name', values='Value')

        # Reset index and rename 'MaterialIndex' to 'MaterialName'
        df_pivot = df_pivot.reset_index()
        df_pivot = df_pivot.rename(columns={'MaterialIndex': 'MaterialName'})

        # Convert ActualWeight from kg to tons
        df_pivot['ActualWeight'] = df_pivot['ActualWeight'].div(1000).round(2)

        # Fetch existing data from MaterialData table
        existing_data = pd.read_sql('SELECT * FROM "MaterialData"', con=engineConRead)
        existing_data = existing_data[['SiloNo', 'MaterialName', 'TotalExtracted']]

        # Merge the DataFrames based on 'MaterialName'
        df_merged = pd.merge(df_pivot, existing_data, on='MaterialName', how='inner')

        # Ensure ActualWeight and TotalExtracted are numeric (converting any non-numeric values to NaN)
        df_merged['ActualWeight'] = pd.to_numeric(df_merged['ActualWeight'], errors='coerce').fillna(0)
        df_merged['TotalExtracted'] = pd.to_numeric(df_merged['TotalExtracted'], errors='coerce').fillna(0)

        # Add ActualWeight and TotalExtracted to create TotalWeight
        df_merged['TotalWeight'] = df_merged['ActualWeight'] + df_merged['TotalExtracted']

        # Display the updated DataFrame
        print("----------------------------------------------------------------------------")
        print(df_merged)

        # Run the update query (parameterized to avoid SQL injection issues)
        for index, row in df_merged.iterrows():
            update_query = """
            UPDATE "MaterialData"
            SET "TotalExtracted" = %s
            WHERE "MaterialName" = %s;
            """
            cursorWrite.execute(update_query, (row['TotalWeight'], row['MaterialName']))

        # Commit the changes
        conn.commit()

        print("TotalWeight values successfully updated in MaterialData table.")
        print("----------------------------------------------------------------------------")
    except Exception as e:
        print("Error occurred:", e)