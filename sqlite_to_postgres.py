import sqlite3
import pandas as pd
from sqlalchemy import create_engine

# SQLite database path
sqlite_db = r"C:\Prolite files\Batch_Report_Flask-main\PLCDB2.db"

# PostgreSQL connection
engine = create_engine(
    "postgresql+psycopg2://postgres:12345678@localhost:5434/PLCDB2"
)

# Connect to SQLite
sqlite_conn = sqlite3.connect(sqlite_db)

# Get all table names
tables = pd.read_sql(
    "SELECT name FROM sqlite_master WHERE type='table';",
    sqlite_conn
)

print("Tables found:")
print(tables)

# Copy tables
for table in tables["name"]:
    print(f"Migrating {table}...")

    df = pd.read_sql(f"SELECT * FROM {table}", sqlite_conn)

    df.to_sql(
        table,
        engine,
        if_exists="replace",
        index=False
    )

print("Migration completed successfully!")