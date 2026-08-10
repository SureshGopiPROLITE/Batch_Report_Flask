import sqlite3
import pandas as pd
from sqlalchemy import create_engine

sqlite_db = r"C:\Users\Prolite_Testing\Downloads\Full_Backup_PLCDB2 (3).db"
engine = create_engine("postgresql+psycopg2://postgres:12345678@localhost:5434/PLCDB2")

sqlite_conn = sqlite3.connect(sqlite_db)

tables = pd.read_sql(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';",
    sqlite_conn
)
print("Tables found:")
print(tables)

for table in tables["name"]:
    print(f"Migrating {table}...")
    try:
        df = pd.read_sql(f'SELECT * FROM "{table}"', sqlite_conn)
        df.to_sql(table, engine, if_exists="replace", index=False, chunksize=5000)
    except Exception as e:
        print(f"  Failed to migrate {table}: {e}")

sqlite_conn.close()
engine.dispose()
print("Migration completed.")