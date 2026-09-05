# from config.postgreGetCon import get_db_connection
from config.sqliteCon import get_db_connection
from werkzeug.security import check_password_hash
from datetime import datetime

def get_user(username):
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Unpack from your sqliteCon method
    conn, cursorRead, cursorWrite = get_db_connection()


    # Fetch the user
    cursorRead.execute('SELECT * FROM users WHERE username = ?', (username,))
    user = cursorRead.fetchone()

    # Update last_login if user found
    if user:
        cursorWrite.execute('UPDATE users SET last_login = ? WHERE username = ?', (current_time, username))
        conn.commit()

    conn.close()
    return user
