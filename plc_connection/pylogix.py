from pylogix import PLC
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
    df[['Value', 'Timestamp']] = df.apply(
            lambda row: pd.Series(readABPLC(plc, row['Tag_name'], row['Data_type'])),
            axis=1
        )
    names = df.loc[df['Value'] == True, 'Name'].tolist()
    print("Active Triggers:", names)
    print(df)
    return names, df


def reset_trigger_tag_ab(plc, tag_name):
    try:
        response = plc.Write(tag_name, False)
        if response.Status == "Success":
            print(f"Trigger reset for tag {tag_name}")
        else:
            print(f"Error resetting tag {tag_name}: {response.Status}")
    except Exception as e:
        print(f"Error in reset_trigger_tag_ab: {e}")
        

def set_tag_ab(plc, tag_name):
    try:
        response = plc.Write(tag_name, True)
        if response.Status == "Success":
            print(f"Trigger reset for tag {tag_name}")
        else:
            print(f"Error resetting tag {tag_name}: {response.Status}")
    except Exception as e:
        print(f"Error in reset_trigger_tag_ab: {e}")


def lifeCounter(plc, df):
    try:
        tag_name = df.loc[0, 'Tag_name']
        read_result = plc.Read(tag_name)

        if read_result.Status != "Success":
            return False

        write_value = read_result.Value
        tag_name_to_write = df.loc[1, 'Tag_name']
        write_result = plc.Write(tag_name_to_write, write_value)

        return write_result.Status == "Success"

    except Exception as e:
        print(f"Error in lifeCounter: {e}")
        return False


def writeinAb(plc, tag_name, write_value):
    try:
        write_result = plc.Write(tag_name, write_value)
        return "Success" if write_result.Status == "Success" else "Error"
    except Exception as e:
        print(f"Error writing to tag '{tag_name}': {e}")
        return "Error"
