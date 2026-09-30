import snap7
import struct
import logging
import pandas as pd
from sqlalchemy import create_engine, text
import datetime
from datetime import datetime               
from snap7.util import set_bool ,set_real, set_int, set_string, set_dint
from snap7.util import get_bool, get_real, get_int, get_dint, get_string
import re
from datetime import datetime


def snap7Connect(plcIP, rack, slot):
    """Returns a connected client, or None if the PLC could not be reached."""
    plc = snap7.client.Client()
    try:
        plc.connect(plcIP, rack, slot)
        return plc
    except Exception as e:
        logging.error(f"Snap7 connect to {plcIP} (rack {rack}, slot {slot}) failed: {e}")
        try:
            plc.destroy()
        except Exception:
            pass
        return None


# ---------------------------------------------------------------
# Tag decoding shared by every read path.
# ---------------------------------------------------------------
def _decode_value(raw, offset, data_type, bit_offset=0):
    dt = str(data_type).upper()

    if dt == "BOOL":
        return (raw[offset] >> int(bit_offset)) & 1
    if dt in ("REAL", "FLOAT"):
        return round(struct.unpack_from(">f", raw, offset)[0], 2)
    if dt == "INT":
        return struct.unpack_from(">h", raw, offset)[0]
    if dt == "WORD":
        return struct.unpack_from(">H", raw, offset)[0]
    if dt == "DINT":
        return struct.unpack_from(">i", raw, offset)[0]
    if dt == "DWORD":
        return struct.unpack_from(">I", raw, offset)[0]
    if dt == "STRING":
        max_len = raw[offset]
        str_len = raw[offset + 1]
        # str_len > max_len is corrupt/misaligned data (bad offset, wrong DB)
        if str_len > max_len:
            return None
        return raw[offset + 2: offset + 2 + str_len].decode("utf-8", errors="ignore").strip()
    return None


def _read_single_tag(plc, db_number, start_offset, data_type, bit_offset=0):
    """One tag, reading only the bytes it occupies (STRING: header first, then its text)."""
    db_number, start_offset = int(db_number), int(start_offset)
    dt = str(data_type).upper()

    if dt == "STRING":
        head = plc.db_read(db_number, start_offset, 2)
        raw = bytearray(head)
        if 0 < head[1] <= head[0]:
            raw += plc.db_read(db_number, start_offset + 2, head[1])
        return _decode_value(raw, 0, dt)

    raw = plc.db_read(db_number, start_offset, _tag_byte_size(dt))
    return _decode_value(raw, 0, dt, bit_offset)


def lifeCounter(plc, df):
    """Heartbeat: copy the value of row 0 (read tag) into row 1 (write tag)."""
    try:
        read_row, write_row = df.iloc[0], df.iloc[1]

        value = _read_single_tag(
            plc, read_row['db_number'], read_row['start_offset'],
            read_row['data_type'], read_row.get('bit_offset', 0))
        if value is None:
            raise ValueError(f"Unsupported read type: {read_row['data_type']}")

        if not writeinSnap7(plc, int(write_row['db_number']), int(write_row['start_offset']),
                            int(write_row.get('bit_offset', 0)), write_row['data_type'], value):
            raise ValueError("life counter write failed")
        return True

    except Exception as e:
        logging.error(f"Error in lifeCounter: {e}")
        return False
    

def monitor_trigger_s7(plc, df):
    """Reads only the given trigger rows. Value is None where a read failed."""
    df = df.copy()
    values = []

    for _, row in df.iterrows():
        try:
            values.append(_read_single_tag(
                plc, row['db_number'], row['start_offset'],
                row['data_type'], row.get('bit_offset', 0)))
        except Exception as e:
            logging.error(f"Trigger read error for {row['Name']}: {e}")
            values.append(None)

    df["Value"] = values
    df["Timestamp"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

    active = [
        name for name, val in zip(df["Name"], df["Value"])
        if val is not None and bool(val)
    ]
    return active, df


def clean_plc_datetime(date_string):
    if pd.isna(date_string) or str(date_string).strip() == "":
        return None

    date_string = re.sub(r"\s*([:-])\s*", r"\1", str(date_string).strip())
    date_string = re.sub(r"\s+", " ", date_string)

    dt = pd.to_datetime(date_string, errors="coerce")
    return None if pd.isna(dt) else dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------
# Byte size needed to read a given PLC data type in one db_read.
# STRING falls back to 256 bytes (254 max-length + 2 header
# bytes), which safely covers every STRING field in this project's
# DB layout (PlantName, RecipeName, MaterialName, etc. are all
# declared as String[254] in TIA Portal). Adjust the fallback if
# a shorter/longer declared string length is ever used elsewhere.
# ---------------------------------------------------------------
def _tag_byte_size(data_type):
    dt = str(data_type).upper()
    return {
        "BOOL": 1,
        "INT": 2,
        "WORD": 2,
        "REAL": 4,
        "DINT": 4,
        "DWORD": 4,
        "FLOAT": 4,
    }.get(dt, 256)  # STRING and anything unrecognized


def _block_end(plc, db_number, db_rows):
    """End offset of one block read covering db_rows. A STRING whose 256-byte
    allowance would stick out past the other tags gets its real size from its
    header byte (declared max length), so the read never runs past the DB end."""
    ends = []
    for _, row in db_rows.iterrows():
        if str(row["data_type"]).upper() != "STRING":
            ends.append(int(row["start_offset"]) + _tag_byte_size(row["data_type"]))
    fixed_end = max(ends, default=0)

    for _, row in db_rows.iterrows():
        if str(row["data_type"]).upper() == "STRING":
            start = int(row["start_offset"])
            size = 256
            if start + size > fixed_end:
                try:
                    size = plc.db_read(db_number, start, 1)[0] + 2
                except Exception:
                    pass
            ends.append(start + size)
    return max(ends)


def read_bulk_plc_data(plc, dfPlcdb):
    """One db_read per DB. If that block read fails (e.g. a trailing STRING's
    256-byte allowance runs past the end of the DB) the DB is read tag by tag
    instead, so one oversized read never loses the whole batch."""
    dfPlcdb = dfPlcdb.copy()
    dfPlcdb["Value"] = None

    if not plc.get_connected():
        logging.error("read_bulk_plc_data: PLC not connected")
        return dfPlcdb

    for db_number in dfPlcdb["db_number"].unique():
        db_rows = dfPlcdb[dfPlcdb["db_number"] == db_number]

        start_offset = int(db_rows["start_offset"].min())
        end_offset = _block_end(plc, int(db_number), db_rows)
        size = end_offset - start_offset

        try:
            raw_data = plc.db_read(int(db_number), start_offset, size)
        except Exception as e:
            logging.warning(f"Block read DB{db_number} ({size} bytes) failed: {e} - reading tag by tag")
            raw_data = None

        for idx, row in db_rows.iterrows():
            try:
                if raw_data is not None:
                    value = _decode_value(
                        raw_data, int(row["start_offset"]) - start_offset,
                        row["data_type"], row.get("bit_offset", 0))
                else:
                    value = _read_single_tag(
                        plc, db_number, row["start_offset"],
                        row["data_type"], row.get("bit_offset", 0))

                # Format PLC Date/Time (an empty/invalid date becomes None)
                if row["Name"] in ["Start Date Time", "End Date Time"]:
                    value = clean_plc_datetime(value)
                elif row["Name"] in ["StartTime", "EndTime"]:
                    value = clean_plc_datetime(value) or (value or "")

                dfPlcdb.at[idx, "Value"] = value

            except Exception as e:
                logging.error(f"Read/decode error DB{db_number} offset {row['start_offset']}: {e}")
                dfPlcdb.at[idx, "Value"] = None

    return dfPlcdb




# -------------------- Bulk Write --------------------
def write_bulk_plc_data(plc, dfPlcdb):

    print(" Bulk PLC write started...")

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
    """Clears the trigger bit. Returns True on success."""
    try:
        data = plc.db_read(int(db_number), int(start_offset), 1)
        set_bool(data, 0, int(bit_offset), False)
        plc.db_write(int(db_number), int(start_offset), data)
        return True
    except Exception as e:
        logging.error(f"Error resetting trigger DB{db_number}, Offset {start_offset}.{bit_offset}: {e}")
        return False


#=========================
# used for recipewrite.py 
#=========================

def set_tag_snap7(plc, db_number, start_offset, bit_offset):
    """Sets a bit to True. Returns True on success."""
    try:
        data = plc.db_read(int(db_number), int(start_offset), 1)
        set_bool(data, 0, int(bit_offset), True)
        plc.db_write(int(db_number), int(start_offset), data)
        return True
    except Exception as e:
        logging.error(f"Error in set_tag_snap7: {e}")
        return False


def readSnap7PLC(plc,db_number,start_offset,data_type='BOOL',bit_offset=0):
  
    try:
        value = _read_single_tag(plc, db_number, start_offset, data_type, bit_offset)
        if str(data_type).upper() == 'BOOL':
            value = bool(value)
        if value is None:
            print(f"Unsupported data type: {data_type}")
            return None, None

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

        return value, timestamp

    except Exception as e:
        print(f"Error reading Siemens PLC tag: {e}")
        return None, None
    
def writeinSnap7(plc, db_number, start_offset, bit_offset, data_type, write_value):
    try:
        data_type = str(data_type).upper()

        if data_type == 'BOOL':
            data = plc.db_read(db_number, start_offset, 1)
            set_bool(data, 0, bit_offset, bool(write_value))
            plc.db_write(db_number, start_offset, data)

        elif data_type in ('REAL', 'FLOAT'):
            data = bytearray(4)
            set_real(data, 0, float(write_value))
            plc.db_write(db_number, start_offset, data)

        elif data_type == 'INT':
            data = bytearray(2)
            set_int(data, 0, int(write_value))
            plc.db_write(db_number, start_offset, data)

        elif data_type == 'WORD':
            plc.db_write(db_number, start_offset, bytearray(struct.pack('>H', int(write_value))))

        elif data_type == 'DINT':
            data = bytearray(4)
            set_dint(data, 0, int(write_value))
            plc.db_write(db_number, start_offset, data)

        elif data_type == 'DWORD':
            plc.db_write(db_number, start_offset, bytearray(struct.pack('>I', int(write_value))))

        elif data_type == 'STRING':
            # Use the length declared in the PLC (first header byte) so a
            # shorter String[n] never overwrites the tags that follow it.
            max_length = plc.db_read(db_number, start_offset, 1)[0] or 254
            text = str(write_value)[:max_length]
            data = bytearray(max_length + 2)
            set_string(data, 0, text, max_length)
            plc.db_write(db_number, start_offset, data)

        else:
            print(f"Unsupported data type: {data_type}")
            return False

        
        return True

    except Exception as e:
        print(f"Error writing Siemens PLC tag: {e}")
        return False
