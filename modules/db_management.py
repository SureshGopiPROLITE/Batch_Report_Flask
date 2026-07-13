"""
modules/db_management.py
"""
import os
import json
from datetime import datetime

DB_PATH = r"C:\Prolite files\Batch_Report_Flask-main\PLCDB2.db"
BACKUP_LOG_PATH = "backup_log.json"
STORAGE_QUOTA_GB = 10


def record_backup_event():
    with open(BACKUP_LOG_PATH, "w") as f:
        json.dump({"last_backup": datetime.now().isoformat()}, f)


def get_last_backup_info():
    if not os.path.exists(BACKUP_LOG_PATH):
        return "No backup yet", "Overdue"

    with open(BACKUP_LOG_PATH) as f:
        data = json.load(f)

    last_backup_time = datetime.fromisoformat(data["last_backup"])

    # Calculate days since last backup
    days_since = (datetime.now() - last_backup_time).days

    if days_since <= 60:      # 2 months 
        status = "Healthy"
    elif days_since <= 180:   # 6 months 
        status = "Warning"
    else:
        status = "Overdue"

    return last_backup_time.strftime("%B %d, %Y %I:%M %p"), status



def get_storage_used_gb():
    size_bytes = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    used_gb = round(size_bytes / (1024 ** 3), 2)
    percent = min(round((used_gb / STORAGE_QUOTA_GB) * 100, 1), 100)
    return used_gb, STORAGE_QUOTA_GB, percent


def get_database_management_data():
    last_backup, status = get_last_backup_info()
    used_gb, quota_gb, percent = get_storage_used_gb()

    return {
        "last_backup": last_backup,
        "status": status,
        "storage_used_gb": used_gb,
        "storage_quota_gb": quota_gb,
        "storage_percent": percent,
    }