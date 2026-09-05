from PyQt5.QtWidgets import  QFileDialog
import pandas as pd


from plc_data_ui import Ui_MainWindow
from database import Sqlite as Sqlite

def openXl():
    file_name, _ = QFileDialog.getOpenFileName(None, "Open Excel File", "", "Excel files (*.xlsx *.xls)")
    if file_name:
        try:
            cursorRead, cursorWrite, engineConRead, engineConWriten, conn = Sqlite.sqlite()
            dfPlcExcel = pd.read_excel(file_name)
            print(dfPlcExcel)
            Sqlite.insert_data_into_sqlite_rec(cursorWrite, conn, dfPlcExcel)
            print("Data inserted into SQLite table successfully.")
            return ("PLC Tag information successfully updated.")
        
        except Exception as e:
            return (f"Error reading Excel file: {e}")
