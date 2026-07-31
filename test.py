import psycopg2

try:
    conn = psycopg2.connect(
        host="localhost",
        port=5434,
        database="PLCDB2",
        user="postgres",
        password="12345678"
    )
    print("Connected successfully!")
    conn.close()
except Exception as e:
    print(e)