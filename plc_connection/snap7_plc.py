import snap7
import struct
import pandas as pd
from sqlalchemy import create_engine, text
import datetime
from datetime import datetime               
from snap7.util import set_bool ,set_real, set_int, set_string, set_dint
from snap7.util import get_bool, get_real, get_int, get_dint, get_string

def snap7Connect(plcIP, rack, slot):
    try:
        print(plcIP, rack, slot)
        plc = snap7.client.Client()
        plc.connect(plcIP, rack, slot)
        return plc
    except Exception as e:
        print(f"Error updating license: {e}")      

def lifeCounter(plc, df):
    try:
        # --- Read source ---
        db_number_r = int(df.loc[0, 'db_number'])
        data_type_r = df.loc[0, 'data_type'].upper()
        start_offset_r = int(df.loc[0, 'start_offset'])
        bit_offset_r = int(df.loc[0].get('bit_offset', 0))

        if data_type_r == 'BOOL':
            raw = plc.db_read(db_number_r, start_offset_r, 1)
            value = (raw[0] >> bit_offset_r) & 1

        elif data_type_r == 'REAL':
            raw = plc.db_read(db_number_r, start_offset_r, 4)
            value = round(struct.unpack('>f', raw)[0], 2)  # ✅ Big-endian

        elif data_type_r == 'INT':
            raw = plc.db_read(db_number_r, start_offset_r, 2)
            value = struct.unpack('>h', raw)[0]  # ✅ Big-endian

        elif data_type_r == 'DINT':
            raw = plc.db_read(db_number_r, start_offset_r, 4)
            value = struct.unpack('>i', raw)[0]  # ✅ Big-endian

        else:
            raise ValueError(f"Unsupported read type: {data_type_r}")

        print("Life Counter (read):", value)

        # --- Write target ---
        db_number_w = int(df.loc[1, 'db_number'])
        data_type_w = df.loc[1, 'data_type'].upper()
        start_offset_w = int(df.loc[1, 'start_offset'])
        bit_offset_w = int(df.loc[1].get('bit_offset', 0))

        if data_type_w == 'BOOL':
            data = plc.db_read(db_number_w, start_offset_w, 1)
            set_bool(data, 0, bit_offset_w, value)
            plc.db_write(db_number_w, start_offset_w, data)

        elif data_type_w == 'REAL':
            data = struct.pack('>f', float(value))
            plc.db_write(db_number_w, start_offset_w, data)

        elif data_type_w == 'INT':
            data = struct.pack('>h', int(value))
            plc.db_write(db_number_w, start_offset_w, data)

        elif data_type_w == 'DINT':
            data = struct.pack('>i', int(value))
            plc.db_write(db_number_w, start_offset_w, data)

        else:
            raise ValueError(f"Unsupported write type: {data_type_w}")

        return True

    except Exception as e:
        print(f"❌ Error in lifeCounter: {e}")
        return False
    

def monitor_trigger_s7(plc, df):

    values = []
    timestamps = []

    for _, row in df.iterrows():

        try:
            db = int(row['db_number'])
            dt = str(row['data_type']).upper()
            start = int(row['start_offset'])
            bit = int(row.get('bit_offset', 0))

            # Read 4 bytes from PLC
            raw = plc.db_read(db, start, 4)

            if dt == "BOOL":
                val = get_bool(raw, 0, bit)

            elif dt in ["INT", "WORD"]:
                val = get_int(raw, 0)

            elif dt in ["REAL", "FLOAT"]:
                val = get_real(raw, 0)

            else:
                val = None

        except Exception as e:
            print(f"Read Error for {row['Name']} : {e}")
            val = None

        values.append(val)

        timestamps.append(
            datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        )

    # Add values to dataframe
    df = df.copy()

    df["Value"] = values
    df["Timestamp"] = timestamps

    # Filter only Trigger category
    df_trigger = df[
        df["Category"] == "Trigger"
    ].copy()


    # Convert values to numeric if possible
    df_trigger["Value"] = pd.to_numeric(
        df_trigger["Value"],
        errors="coerce"
    )

    # Active trigger list
    active = []

    for _, row in df_trigger.iterrows(): 

        # Trigger active if value is non-zero or True
        if pd.notna(row["Value"]) and bool(row["Value"]):
            active.append(row["Name"])

    
    return active, df_trigger

def read_bulk_plc_data(plc, dfPlcdb):
    dfPlcdb = dfPlcdb.copy()
    dfPlcdb["Value"] = None
    

    if not plc.get_connected():
        print("⚠️ PLC not connected!")
        return dfPlcdb

    for db_number in dfPlcdb["db_number"].unique():
        db_rows = dfPlcdb[dfPlcdb["db_number"] == db_number]

        start_offset = int(db_rows["start_offset"].min())
        end_offset = int(db_rows["start_offset"].max()) + 6  # +6 to cover REAL/DINT size
        size = end_offset - start_offset

        try:
            raw_data = plc.db_read(int(db_number), start_offset, size)
        except Exception as e:
            print(f"❌ Failed to read DB{db_number}: {e}")
            continue

        # Decode each tag from the buffer using same logic as single read
        for idx, row in db_rows.iterrows():
            local_offset = int(row["start_offset"]) - start_offset
            try:
                dt = row["data_type"].upper()
                if dt == "BOOL":
                    dfPlcdb.at[idx, "Value"] = (raw_data[local_offset] >> int(row.get("bit_offset", 0))) & 1
                elif dt == "REAL":
                    dfPlcdb.at[idx, "Value"] = round(struct.unpack_from(">f", raw_data, local_offset)[0], 2)
                elif dt == "INT":
                    dfPlcdb.at[idx, "Value"] = struct.unpack_from(">h", raw_data, local_offset)[0]
                elif dt == "DINT":
                    dfPlcdb.at[idx, "Value"] = struct.unpack_from(">i", raw_data, local_offset)[0]
                elif dt == "STRING":
                    max_len = raw_data[local_offset]
                    str_len = raw_data[local_offset + 1]
                    if 0 < str_len <= max_len:
                        dfPlcdb.at[idx, "Value"] = raw_data[local_offset+2:local_offset+2+str_len].decode("utf-8", errors="ignore")
                    else:
                        dfPlcdb.at[idx, "Value"] = ""
                else:
                    dfPlcdb.at[idx, "Value"] = None
            except Exception as e:
                print(f"⚠️ Decode error DB{db_number} offset {row['start_offset']}: {e}")
                dfPlcdb.at[idx, "Value"] = None
                
    return dfPlcdb



# -------------------- Bulk Write --------------------
def write_bulk_plc_data(plc, dfPlcdb):

    print("⚡ Bulk PLC write started...")

    if not plc.get_connected():
        return {
            "success": False,
            "message": "PLC not connected."
        }

    errors = []
    success_count = 0

    for db_number in dfPlcdb["db_number"].unique():

        db_rows = dfPlcdb[dfPlcdb["db_number"] == db_number]

        start_offset = int(db_rows["start_offset"].min())

        max_end = start_offset

        for _, row in db_rows.iterrows():

            dt = str(row["data_type"]).upper()
            offset = int(row["start_offset"])

            if dt == "BOOL":
                end = offset + 1
            elif dt in ["INT", "WORD"]:
                end = offset + 2
            elif dt in ["REAL", "DINT", "DWORD", "FLOAT"]:
                end = offset + 4
            elif dt == "STRING":
                end = offset + 22
            else:
                end = offset + 1

            max_end = max(max_end, end)

        size = max_end - start_offset

        try:
            buffer = bytearray(
                plc.db_read(
                    int(db_number),
                    start_offset,
                    size
                )
            )

        except Exception as e:

            msg = f"Failed to read DB{db_number}: {e}"
            print(f"❌ {msg}")
            errors.append(msg)
            continue

        for _, row in db_rows.iterrows():

            try:

                dt = str(row["data_type"]).upper()
                value = row["Value"]

                local_offset = (
                    int(row["start_offset"])
                    - start_offset
                )

                if dt == "BOOL":

                    set_bool(
                        buffer,
                        local_offset,
                        int(row.get("bit_offset", 0)),
                        bool(value)
                    )

                elif dt == "INT":

                    buffer[
                        local_offset:local_offset + 2
                    ] = struct.pack(">h", int(value))

                elif dt == "WORD":

                    buffer[
                        local_offset:local_offset + 2
                    ] = struct.pack(">H", int(value))

                elif dt in ["REAL", "FLOAT"]:

                    buffer[
                        local_offset:local_offset + 4
                    ] = struct.pack(">f", float(value))

                elif dt == "DINT":

                    buffer[
                        local_offset:local_offset + 4
                    ] = struct.pack(">i", int(value))

                elif dt == "DWORD":

                    buffer[
                        local_offset:local_offset + 4
                    ] = struct.pack(">I", int(value))

                elif dt == "STRING":

                    text = str(value)

                    max_len = 20

                    if len(text) > max_len:
                        text = text[:max_len]

                    string_data = bytearray(max_len + 2)
                    string_data[0] = max_len
                    string_data[1] = len(text)
                    string_data[2:2 + len(text)] = text.encode(
                        "ascii",
                        errors="ignore"
                    )

                    buffer[
                        local_offset:local_offset + max_len + 2
                    ] = string_data

            except Exception as e:

                msg = (
                    f"Encode error DB{db_number} "
                    f"offset {row['start_offset']}: {e}"
                )

                print(f"⚠️ {msg}")
                errors.append(msg)

        try:

            plc.db_write(
                int(db_number),
                start_offset,
                buffer
            )

            success_count += 1

            print(
                f"✅ DB{db_number} written "
                f"({size} bytes)"
            )

        except Exception as e:

            msg = f"Failed to write DB{db_number}: {e}"

            print(f"❌ {msg}")
            errors.append(msg)

    if errors:
        return {
            "success": False,
            "message": "PLC write completed with errors.",
            "errors": errors
        }

    return {
        "success": True,
        "message": f"Recipe Written Successfully"
    }




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
        print(f"ParNters - db_number: {db_number}, start_offset: {start_offset}, data_type: {data_type}, bit_offset: {bit_offset}")
    except struct.error as se:
        print(f"struct.error occurred: {se}")
        print(f"Reading: {reading}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        print(f"Parameters - db_number: {db_number}, start_offset: {start_offset}, data_type: {data_type}, bit_offset: {bit_offset}")

def reset_trigger_tag_s7(plc, db_number, start_offset, bit_offset=0):
    try:
        data = plc.db_read(db_number, start_offset, 1)
        set_bool(data, 0, bit_offset, False)
        plc.db_write(db_number, start_offset, data)
    except Exception as e:
        print(f"❌ Error resetting trigger DB{db_number}, Offset {start_offset}.{bit_offset}: {e}")


#=========================
# used for recipewrite.py 
#=========================

def set_tag_snap7(plc, db_number, start_offset, bit_offset):
    try:
        # Read the existing byte
        data = plc.db_read(db_number, start_offset, 1)
        # Set the required bit to True
        set_bool(data, 0, bit_offset, True)
        # Write back to PLC
        plc.db_write(db_number, start_offset, data)
        
    except Exception as e:
        print(f"Error in set_tag_snap7: {e}")


def readSnap7PLC(plc,db_number,start_offset,data_type='BOOL',bit_offset=0):
  
    try:
        db_number = int(db_number)
        start_offset = int(start_offset)
        bit_offset = int(bit_offset)
        data_type = str(data_type).upper()

        if data_type == 'BOOL':
            data = plc.db_read(db_number, start_offset, 1)
            value = get_bool(data, 0, bit_offset)

        elif data_type == 'REAL':
            data = plc.db_read(db_number, start_offset, 4)
            value = round(get_real(data, 0), 2)

        elif data_type == 'INT':
            data = plc.db_read(db_number, start_offset, 2)
            value = get_int(data, 0)

        elif data_type == 'DINT':
            data = plc.db_read(db_number, start_offset, 4)
            value = get_dint(data, 0)

        elif data_type == 'STRING':
            # Adjust length according to PLC declaration
            data = plc.db_read(db_number, start_offset, 256)
            value = get_string(data, 0)

        else:
            print(f"Unsupported data type: {data_type}")
            return None, None

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

        return value, timestamp

    except Exception as e:
        print(f"Error reading Siemens PLC tag: {e}")
        return None, None
    
def writeinSnap7(plc, db_number, start_offset, bit_offset, data_type, write_value):
    try:

        if data_type == 'BOOL':
            data = plc.db_read(db_number, start_offset, 1)
            set_bool(data, 0, bit_offset, bool(write_value))
            plc.db_write(db_number, start_offset, data)

        elif data_type == 'REAL':
            data = bytearray(4)
            set_real(data, 0, float(write_value))
            plc.db_write(db_number, start_offset, data)

        elif data_type == 'INT':
            data = bytearray(2)
            set_int(data, 0, int(write_value))
            plc.db_write(db_number, start_offset, data)

        elif data_type == 'DINT':
            data = bytearray(4)
            set_dint(data, 0, int(write_value))
            plc.db_write(db_number, start_offset, data)

        elif data_type == 'STRING':
            max_length = 254
            data = bytearray(max_length + 2)
            set_string(data, 0, str(write_value), max_length)
            plc.db_write(db_number, start_offset, data)

        else:
            print(f"Unsupported data type: {data_type}")
            return False

        
        return True

    except Exception as e:
        print(f"Error writing Siemens PLC tag: {e}")
        return False