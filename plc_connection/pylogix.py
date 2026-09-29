from pylogix import PLC
import logging
import pandas as pd
from datetime import datetime

def connectABPLC(plc_ip):
    try:
        plc = PLC()
        plc.IPAddress = plc_ip
        return plc
    except Exception as e:
        print(f"Error connecting to AB PLC: {e}")
        return None
    
def readABPLC_bulk(plc, tag_list):
    """
    Reads a list of tags from AB PLC using pylogix and returns results and timestamp.
    """
    try:
        results = plc.Read(tag_list)  # Bulk read
        if not isinstance(results, list):
            results = [results]
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        return results, timestamp
    except Exception as e:
        logging.error(f"Error reading PLC tags: {e}")
        return [], None
    
def readABPLC(plc, tag_name, data_type):
    try:
        result = plc.Read(tag_name)
        
        if result.Status != "Success":
            return None, None

        if data_type == 'BOOL':
            value = bool(result.Value)
        elif data_type == 'REAL':
            value = round(float(result.Value), 2)
        elif data_type == 'INT' or data_type == 'DINT':
            value = int(result.Value)
        elif data_type == 'STRING':
            value = result.Value
        else:
            return None, None

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return value, timestamp

    except Exception as e:
        print(f"Error reading tag {tag_name}: {e}")
        return None, None


def monitor_trigger_ab(plc, df):
    """Reads the given trigger rows in one request. Value is None where a read failed."""
    df = df.copy()
    results, timestamp = readABPLC_bulk(plc, df["Tag_name"].tolist())
    values = {r.TagName: r.Value for r in results if r.Status == "Success"}

    df["Value"] = [values.get(tag) for tag in df["Tag_name"]]
    df["Timestamp"] = timestamp

    names = [
        name for name, val in zip(df["Name"], df["Value"])
        if val is not None and bool(val)
    ]
    return names, df


def reset_trigger_tag_ab(plc, tag_name):
    """Writes False to the trigger. Returns True on success."""
    try:
        response = plc.Write(tag_name, False)
        if response.Status == "Success":
            return True
        logging.error(f"Error resetting tag {tag_name}: {response.Status}")
    except Exception as e:
        logging.error(f"Error in reset_trigger_tag_ab: {e}")
    return False
        

def set_tag_ab(plc, tag_name):
    """Writes True to the tag. Returns True on success."""
    try:
        response = plc.Write(tag_name, True)
        if response.Status == "Success":
            return True
        logging.error(f"Error setting tag {tag_name}: {response.Status}")
    except Exception as e:
        logging.error(f"Error in set_tag_ab: {e}")
    return False

def lifeCounter(plc, df):
    """Heartbeat: copy the value of row 0 (read tag) into row 1 (write tag)."""
    try:
        read_result = plc.Read(df.iloc[0]['Tag_name'])

        if read_result.Status != "Success":
            logging.error(f"Life counter read failed: {read_result.Status}")
            return False

        write_result = plc.Write(df.iloc[1]['Tag_name'], read_result.Value)
        if write_result.Status != "Success":
            logging.error(f"Life counter write failed: {write_result.Status}")
            return False
        return True

    except Exception as e:
        logging.error(f"Error in lifeCounter: {e}")
        return False

def writeinAb(plc, tag_name, write_value):
    try:
        write_result = plc.Write(tag_name, write_value)
        return "Success" if write_result.Status == "Success" else "Error"
    except Exception as e:
        print(f"Error writing to tag '{tag_name}': {e}")
        return "Error"
