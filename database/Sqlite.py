from sqlalchemy import create_engine
import shutil
import os
from tkinter import Tk, filedialog
import sqlite3
import pandas as pd
from datetime import datetime, timedelta



def sqlite():
    conn = sqlite3.connect('PLCDB2.db')
    cursorRead = conn.cursor()
    cursorWrite = conn.cursor()
    engine = create_engine('sqlite:///PLCDB2.db')
    engineConRead = engine.connect()
    engineConWrite = engine.connect()
    return cursorRead, cursorWrite, engineConRead, engineConWrite, conn



def calculate_silo_diff(dfPlcdb: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate absolute difference (kg) and percentage between SetWeight and ActualWeight for each Silo.
    Returns dfPlcdb with new rows added.
    """
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
        # Hide the root window
        root = Tk()
        root.withdraw()
        
        # Default database file path
        default_database_path = os.path.abspath("PLCDB2.db")
        
        # Check if the default database file exists
        if not os.path.exists(default_database_path):
            print(f"Default database file '{default_database_path}' does not exist.")
            return
        
        # Ask the user to select the destination for the backup
        backup_path = filedialog.asksaveasfilename(
            title="Select Backup Destination", 
            defaultextension=".db", 
            filetypes=[("SQLite Database Files", "*.db")]
        )
        if not backup_path:
            print("No backup destination selected.")
            return

        # Ensure the backup path directory exists, create if it does not
        backup_dir = os.path.dirname(backup_path)
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        
        # Perform the backup by copying the database file
        shutil.copy2(default_database_path, backup_path)
        
        print(f"Backup of database '{default_database_path}' completed successfully.")
    
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
    

def showBatch(conn, hours, from_time, to_time, engineConRead):
    try:
        if hours == "Custom":
            print("Time:", from_time, to_time)

            from_time_dt = datetime.fromisoformat(from_time)
            to_time_dt = datetime.fromisoformat(to_time)
            date_diff = to_time_dt - from_time_dt
            print("Date Difference:", date_diff.days)    

            query = f"""
            SELECT DISTINCT * FROM Batches
            WHERE TimeStamp BETWEEN '{from_time.replace("T", " ")}' AND '{to_time.replace("T", " ")}'
            ORDER BY TimeStamp ASC;
            """

        elif hours in ["1 Hr", "4 Hr", "8 Hr", "12 Hr", "24 Hr"]:
            hours_mapping = {"1 Hr": 1, "4 Hr": 4, "8 Hr": 8, "12 Hr": 12, "24 Hr": 24}
            from_time_dt = datetime.now() - timedelta(hours=hours_mapping[hours])
            from_time = from_time_dt.strftime('%Y-%m-%d %H:%M:%S')

            query = f"""
            SELECT DISTINCT * FROM Batches
            WHERE TimeStamp >= '{from_time}'
            ORDER BY TimeStamp ASC;
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
        drop_table_query = "DROP TABLE IF EXISTS Data"
        cursor.execute(drop_table_query)
        print("Dropped existing table (if any).")

        # Determine SQLite data types
        sqlite_types = {
            'int64': 'INTEGER',
            'float64': 'REAL',
            'bool': 'BOOLEAN',
            'object': 'TEXT'
        }

        # Dynamically generate CREATE TABLE query based on DataFrame columns
        columns = ', '.join([f"{col} {sqlite_types[str(dfPlcExcel[col].dtype)]}" for col in dfPlcExcel.columns])
        create_table_query = f"""
            CREATE TABLE IF NOT EXISTS Data ({columns});
        """
        cursor.execute(create_table_query)
        print("Created new table based on DataFrame columns.")

        # Insert data into the table
        for index, row in dfPlcExcel.iterrows():
            placeholders = ', '.join(['?' for _ in dfPlcExcel.columns])
            columns = ', '.join(dfPlcExcel.columns)
            sql = f"INSERT INTO Data ({columns}) VALUES ({placeholders})"
            cursor.execute(sql, tuple(row))
        print("Inserted data into the new table.")
        
        # Commit changes
        conn.commit()
        print("Data committed to SQLite database.")
        
    except Exception as e:
        print(f"Error occurred: {str(e)}")
    
def dfPlc(conn, softwaretype):
    try:
        print("Inside the DF")
        print("Software Type:", softwaretype)
        select_query = "SELECT * FROM Data"
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
    cursorRead, cursorWrite, engineConRead, engineConWriten, conn = sqlite()

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

    # ✅ Round to 2 decimal places
    df_weight["Value"] = pd.to_numeric(
        df_weight["Value"],
        errors="coerce"
    )

    df_weight["Value"] = df_weight["Value"].round(2)

    df_weight.rename(columns={"Value": "Total Batch Weight"}, inplace=True)

    # Merge with df_pivot_1
    df_pivot_1 = df_pivot_1.merge(df_weight, on="BatchNo", how="left")

    # Insert df_pivot_1 into the Batches table
    df_pivot_1.to_sql("Batches", con=engineConWriten, if_exists="append", index=False)

   


#recipe tag inc
def insert_data_into_sqlite_rec(cursor, conn, dfPlcExcel):
    try:
        # Drop the table if it exists
        drop_table_query = "DROP TABLE IF EXISTS RecipeTagName"
        cursor.execute(drop_table_query)
        print("Dropped existing table (if any).")

        # Determine SQLite data types
        sqlite_types = {
            'int64': 'INTEGER',
            'float64': 'REAL',
            'bool': 'BOOLEAN',
            'object': 'TEXT'
        }

        # Dynamically generate CREATE TABLE query based on DataFrame columns
        columns = ', '.join([f"{col} {sqlite_types[str(dfPlcExcel[col].dtype)]}" for col in dfPlcExcel.columns])
        create_table_query = f"""
            CREATE TABLE IF NOT EXISTS RecipeTagName ({columns});
        """
        cursor.execute(create_table_query)
        print("Created new table based on DataFrame columns.")

        # Insert data into the table
        for index, row in dfPlcExcel.iterrows():
            placeholders = ', '.join(['?' for _ in dfPlcExcel.columns])
            columns = ', '.join(dfPlcExcel.columns)
            sql = f"INSERT INTO RecipeTagName ({columns}) VALUES ({placeholders})"
            cursor.execute(sql, tuple(row))
        print("Inserted data into the new table.")
        
        # Commit changes
        conn.commit()
        print("Data committed to SQLite database.")
        
    except Exception as e:
        print(f"Error occurred: {str(e)}")



import pandas as pd
from sqlalchemy import create_engine, inspect

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
        existing_data = pd.read_sql('SELECT * FROM MaterialData', con=engineConRead)
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

        # Assuming you have an established connection with cursor (cursorWrite), run the update query
        for index, row in df_merged.iterrows():
            # Prepare the update query
            update_query = f"""
            UPDATE MaterialData
            SET TotalExtracted = {row['TotalWeight']}
            WHERE MaterialName = '{row['MaterialName']}';
            """
            # Execute the update query
            cursorWrite.execute(update_query)

        # Commit the changes
        conn.commit()

        print("TotalWeight values successfully updated in MaterialData table.")
        print("----------------------------------------------------------------------------")
    except Exception as e:
        print("Error occurred:", e)






