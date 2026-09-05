import snap7
import struct
# import pandas as pd
from sqlalchemy import create_engine, text
import datetime
from datetime import datetime               
      


def snap7Connect(plcIP, rack, slot):
    try:
        print(plcIP, rack, slot)
        plc = snap7.client.Client()
        plc.connect(plcIP, rack, slot)
        return plc
    except Exception as e:
        print(f"Error updating license: {e}")


def plcDataSnap7(plc, db_number, data_type, start_offset, bit_offset):
    try:
        if data_type == 'BOOL':
            reading = plc.db_read(db_number, start_offset, 1)
            value = snap7.util.get_bool(reading, 0, bit_offset)
        elif data_type == 'REAL':
            reading = plc.db_read(db_number, start_offset, 4)
            value = round(struct.unpack('>f', reading)[0], 2)
        elif data_type == 'INT':
            reading = plc.db_read(db_number, start_offset, 2)
            value = struct.unpack('>h', reading)[0]
        elif data_type == 'DINT':  # Add support for double integer (4 bytes)
            reading = plc.db_read(db_number, start_offset, 4)
            value = struct.unpack('>i', reading)[0]
        elif data_type == 'STRING':  # Add support for STRING
            max_length = plc.db_read(db_number, start_offset, 1)[0]  # Read max length
            str_length = plc.db_read(db_number, start_offset + 1, 1)[0]  # Read current length
            string_data = plc.db_read(db_number, start_offset + 2, str_length)  # Read the actual string data
            value = string_data.decode('utf-8')  # Convert bytes to string
            
        else:   
            print("Unsupported data type:", data_type)
            return None
        print("GETED VALUE@@@@@@@@@@@@@@@@@@@@@@@@@@@@@", value)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        return value, timestamp

    except TypeError as te:
        print(f"TypeError occurred: {te}")
        print(f"Parameters - db_number: {db_number}, start_offset: {start_offset}, data_type: {data_type}, bit_offset: {bit_offset}")
    except struct.error as se:
        print(f"struct.error occurred: {se}")
        print(f"Reading: {reading}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        print(f"Parameters - db_number: {db_number}, start_offset: {start_offset}, data_type: {data_type}, bit_offset: {bit_offset}")

