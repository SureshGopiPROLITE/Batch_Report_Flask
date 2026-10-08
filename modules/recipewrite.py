"""Recipe download to the PLC.

prepare_download() works out everything that will be written - the recipe
steps in the user's order, the PLC slot each goes to, the header values and
the PLC driver/address - without touching the PLC. The confirmation popup
shows that (preview_download) and writePlcRecipe() writes exactly the same.

Driver 1 = Siemens (snap7, tags addressed by db_number/start_offset/data_type)
Driver 2 = Rockwell (pylogix, tags addressed by Tag_name)
"""
import logging
import time

import pandas as pd
from sqlalchemy import text

from config import sqliteCon
from modules import monitor
from plc_connection import snap7_plc, pylogix

DRIVER_SIEMENS, DRIVER_ROCKWELL = monitor.DRIVER_SIEMENS, monitor.DRIVER_ROCKWELL

# Columns each driver needs in the uploaded Recipe Tag table
TAG_COLUMNS = {
    DRIVER_SIEMENS: ["db_number", "start_offset", "data_type"],
    DRIVER_ROCKWELL: ["Tag_name"],
}

# Recipe rows in the order the user arranged them (Seq), oldest first for rows
# saved before ordering existed. Rows without a silo are unfinished and skipped.
# MaterialName = what is in that silo now (Stocks), exactly as the recipe
# table on screen shows it; the name stored in the recipe is only a fallback.
RECIPE_STEPS_SQL = text(
    'SELECT r."Index", r."Seq", r."Category", r."SiloNo", '
    'COALESCE(m."MaterialName", r."MaterialName") AS "MaterialName", '
    'r."SetWeight", r."FineWeight", r."Tolerance", COALESCE(r."InflightWeight", 0) AS "InflightWeight", '
    'r."CoarseSpeed", r."FineSpeed" '
    'FROM "recipeData" r LEFT JOIN "MaterialData" m ON r."SiloNo" = m."SiloNo" '
    'WHERE r."Category" = :category AND r."SiloNo" IS NOT NULL '
    'ORDER BY r."Seq" NULLS LAST, r."Index"')

WEIGHT_COLUMNS = ["SetWeight", "FineWeight", "Tolerance", "InflightWeight", "CoarseSpeed", "FineSpeed"]


class DownloadError(ValueError):
    """A problem the operator can fix; the message is shown as-is."""


def _failed_tags(df, ok_value):
    """Names of the tags whose write Status is not ok_value."""
    return df.loc[df["Status"] != ok_value, "Name"].tolist()


def load_recipe_steps(engineConRead, recipe_name):
    """The recipe's rows in download order, numbered Step 1..n."""
    dfRecipe = pd.read_sql_query(RECIPE_STEPS_SQL, engineConRead, params={"category": recipe_name})
    # Two Stocks rows for one silo must not turn one recipe row into two steps
    dfRecipe = dfRecipe.drop_duplicates(subset="Index", keep="first").reset_index(drop=True)
    dfRecipe.insert(0, "Step", range(1, len(dfRecipe) + 1))
    return dfRecipe


def map_steps_to_slots(dfTags, dfRecipe):
    """Each silo's values go to that silo's own tags: a recipe row for silo 3
    -> Recipe_Data[3].SiloNo / .SetWeight / .InflightWeight / ... and its
    position in the recipe (Order column on screen) -> Recipe_Data[3].Order.

    The PLC uses the Order tag for the dosing sequence. Silos not in the recipe
    get Order 0, SiloNo 0, weights 0 and MaterialName "." so the PLC skips them.
    Only tags listed in the recipe tag table are written (no Order /
    InflightWeight tag there -> that value is simply not sent)."""
    tags = dfTags.rename(columns={"SiloNo": "Slot"}).astype({"Slot": "int"})
    available = set(tags["Slot"])

    silos = pd.to_numeric(dfRecipe["SiloNo"], errors="coerce").astype("Int64")
    twice = sorted(int(x) for x in silos[silos.duplicated()].dropna().unique())
    if twice:
        raise DownloadError(f"Silo {', '.join(map(str, twice))} is used more than once in this recipe - "
                            "each silo has one PLC address, so combine those rows")
    missing = sorted(int(x) for x in silos.dropna() if int(x) not in available)
    if missing:
        raise DownloadError(f"No PLC address for silo {', '.join(map(str, missing))} in the recipe tag table "
                            f"(it has Recipe_Data[{min(available)}..{max(available)}])")

    steps = dfRecipe.assign(Slot=silos.astype(int), Order=dfRecipe["Step"])
    steps = steps.drop(columns=[c for c in ("Index", "Category", "Seq", "Step") if c in steps.columns])
    merged = tags.merge(steps, on="Slot", how="left")

    merged["SiloNo"] = pd.to_numeric(merged["SiloNo"], errors="coerce").fillna(0).astype(int)
    merged["Order"] = pd.to_numeric(merged["Order"], errors="coerce").fillna(0).astype(int)
    merged["MaterialName"] = merged["MaterialName"].fillna(".")
    for col in WEIGHT_COLUMNS:
        if col not in merged.columns:
            merged[col] = 0
    merged[WEIGHT_COLUMNS] = merged[WEIGHT_COLUMNS].fillna(0)

    known = set(merged.columns)
    unknown = sorted(set(merged["Name"]) - known)
    if unknown:
        raise DownloadError(f"Recipe tag table has tag name(s) the recipe has no value for: {unknown}. "
                            f"Use: SiloNo, MaterialName, Order, {', '.join(WEIGHT_COLUMNS)}")
    merged["Value"] = merged.apply(lambda row: row[row["Name"]], axis=1)
    return merged


def _plain(value):
    """numpy scalars -> Python values (pylogix / snap7 helpers expect them).
    Whole numbers become int: pandas turns 50 into 50.0, and pylogix cannot
    pack a float into a DINT tag (an int packs fine into a REAL tag)."""
    value = value.item() if hasattr(value, "item") else value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


MIXER_MIN, MIXER_MAX = 1, 6   # mixers available at the plant


def prepare_download(mixerno, recipe_name, driver):
    """Everything to be written, as DataFrames + a summary. Raises DownloadError."""
    driver = int(driver)
    if driver not in monitor.DRIVER_NAMES:
        raise DownloadError(f"Unknown PLC driver {driver} - choose Siemens or Rockwell in Settings")
    if not recipe_name:
        raise DownloadError("Recipe name not selected.")
    try:
        mixerno = int(mixerno)
    except (TypeError, ValueError):
        raise DownloadError("Mixer No must be a whole number")
    if not MIXER_MIN <= mixerno <= MIXER_MAX:
        raise DownloadError(f"Mixer No must be from {MIXER_MIN} to {MIXER_MAX}")

    engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()
    if engineConRead is None:
        raise DownloadError("Database not reachable")
    try:
        dfTags = pd.read_sql_query(text('SELECT * FROM "RecipeTagName"'), engineConRead)
        dfRecipe = load_recipe_steps(engineConRead, recipe_name)
        dfInfo = pd.read_sql_query(text('SELECT * FROM "Info_db"'), engineConRead)
    finally:
        engineConRead.close()
        engineConWrite.close()

    if dfTags.empty:
        raise DownloadError("Recipe tag table is empty - upload it in Settings (Recipe DB)")
    missing_cols = [c for c in TAG_COLUMNS[driver] if c not in dfTags.columns]
    if missing_cols:
        other = DRIVER_ROCKWELL if driver == DRIVER_SIEMENS else DRIVER_SIEMENS
        hint = (f" It looks like a {monitor.DRIVER_NAMES[other]} tag table."
                if all(c in dfTags.columns for c in TAG_COLUMNS[other]) else "")
        raise DownloadError(
            f"Settings driver is {monitor.DRIVER_NAMES[driver]}, but the uploaded recipe tag table "
            f"has no {', '.join(missing_cols)} column.{hint} Upload the {monitor.DRIVER_NAMES[driver]} "
            f"recipe tag table or change the driver in Settings.")

    if dfRecipe.empty:
        raise DownloadError(f"No recipe data found for '{recipe_name}'.")

    # Header / control tags have a word in SiloNo (Read, Write, Header)
    is_header = dfTags["SiloNo"].astype(str).str.isalpha()
    dfHeader = dfTags[is_header].copy()
    dfSlots = dfTags[~is_header].reset_index(drop=True)

    tagReadReady = dfHeader[dfHeader["SiloNo"] == "Read"]
    tagWriteDwn = dfHeader[dfHeader["SiloNo"] == "Write"]
    if tagReadReady.empty:
        raise DownloadError("ReadyToReceiveRecipe tag (SiloNo = Read) not configured in the recipe tag table.")
    if tagWriteDwn.empty:
        raise DownloadError("RecipeDownloaded tag (SiloNo = Write) not configured in the recipe tag table.")
    dfHeader = dfHeader[~dfHeader["SiloNo"].isin(["Read", "Write"])].reset_index(drop=True)

    dfRecipeTags = map_steps_to_slots(dfSlots, dfRecipe)

    plant = monitor.info_value(dfInfo, "Company_Name", "") or ""
    dfHeader["Value"] = None
    dfHeader.loc[dfHeader["Name"] == "PlantName", "Value"] = str(plant)
    dfHeader.loc[dfHeader["Name"] == "RecipeName", "Value"] = str(recipe_name)
    dfHeader.loc[dfHeader["Name"] == "MixerSelected", "Value"] = (
        str(mixerno) if driver == DRIVER_SIEMENS else mixerno)
    unknown = dfHeader.loc[dfHeader["Value"].isna(), "Name"].tolist()
    if unknown:
        raise DownloadError(f"Header tag(s) in the recipe tag table have no value: {unknown} "
                            "(expected PlantName, RecipeName, MixerSelected)")

    node = monitor.info_value(dfInfo, "Plc_IP") or dfInfo.loc[0, "Info"]
    node = str(node).strip()
    ip, rack, slot = monitor.parse_node(driver, node)    # DownloadError-worthy ValueError

    tag_names = set(dfSlots["Name"])
    steps = [
        {"step": int(r.Step), "silo": _plain(r.SiloNo), "material": r.MaterialName,
         "set_weight": _plain(r.SetWeight), "fine_weight": _plain(r.FineWeight),
         "tolerance": _plain(r.Tolerance), "inflight": _plain(r.InflightWeight),
         "address": f"Recipe_Data[{_plain(r.SiloNo)}]"}
        for r in dfRecipe.itertuples()
    ]
    summary = {
        "recipe": recipe_name, "mixer": mixerno, "plant": plant,
        "driver": driver, "driver_name": monitor.DRIVER_NAMES[driver], "plc": node,
        "steps": steps, "slots": int(dfSlots["SiloNo"].astype(int).max()),
        # tags the PLC table lacks, so the popup can say they are not sent
        "not_sent": [t for t in ("Order", "InflightWeight") if t not in tag_names],
    }
    return {
        "summary": summary, "driver": driver, "ip": ip, "rack": rack, "slot": slot,
        "dfRecipeTags": dfRecipeTags, "dfHeader": dfHeader,
        "tagReadReady": tagReadReady.iloc[0], "tagWriteDwn": tagWriteDwn.iloc[0],
    }


def preview_download(mixerno, recipe_name, driver):
    """What the confirmation popup shows. Does not touch the PLC."""
    try:
        return {"success": True, **prepare_download(mixerno, recipe_name, driver)["summary"]}
    except (DownloadError, ValueError) as e:
        return {"success": False, "message": str(e)}


# ---------------------------------------------------------------------------
# PLC writes
# ---------------------------------------------------------------------------
def _s7_address(row):
    bit = 0 if pd.isna(row.get("bit_offset")) else int(float(row["bit_offset"]))
    return int(row["db_number"]), int(float(row["start_offset"])), bit


def _write_siemens(p):
    plc = snap7_plc.snap7Connect(p["ip"], p["rack"], p["slot"])
    if plc is None:
        return {"success": False, "message": f"Unable to connect to the Siemens PLC ({p['summary']['plc']})."}
    try:
        status = plc.get_cpu_state()
        if status != "S7CpuStatusRun":
            return {"success": False, "message": f"PLC not in RUN mode. Current state: {status}"}

        ready_row = p["tagReadReady"]
        db, start, bit = _s7_address(ready_row)
        ready, _ = snap7_plc.readSnap7PLC(plc, db, start, ready_row["data_type"], bit)
        if not ready:
            return {"success": False, "message": "Siemens PLC is not ready to receive a recipe (ReadyToReceive is off)."}

        def write(row):
            db, start, bit = _s7_address(row)
            return snap7_plc.writeinSnap7(plc, db, start, bit, row["data_type"], _plain(row["Value"]))

        # Address order; the dosing sequence travels in the Order tags
        tags = p["dfRecipeTags"].sort_values("Slot", kind="stable")
        tags["Status"] = tags.apply(write, axis=1)
        header = p["dfHeader"]
        header["Status"] = header.apply(write, axis=1)

        failed = _failed_tags(tags, True) + _failed_tags(header, True)
        if failed:
            return {"success": False, "message": f"Recipe NOT downloaded - PLC write failed for: {failed}"}

        db, start, bit = _s7_address(p["tagWriteDwn"])
        if not snap7_plc.set_tag_snap7(plc, db, start, bit):
            return {"success": False, "message": "Recipe values written but the RecipeDownloaded bit could not be set"}
        return None
    finally:
        try:
            plc.disconnect()
        except Exception:
            pass


def _write_rockwell_bulk(plc, df):
    """Writes every row's Value to its Tag_name; returns the Status per row
    ("Success" or the error). One list write: pylogix reads the unknown tag
    types in batches and packs many writes into each request. Writing tag by
    tag cost two network round trips per tag (type lookup + write) - fast
    against a simulator, 10 s+ for a full recipe on a real plant network.
    Tags the batch could not write are retried one by one."""
    items = [(str(t), _plain(v)) for t, v in zip(df["Tag_name"], df["Value"])]
    if not items:
        return []
    try:
        results = plc.Write(items)
        if not isinstance(results, list):
            results = [results]
        status = {r.TagName: r.Status for r in results}
    except Exception as e:
        logging.warning(f"Rockwell bulk write failed ({e}) - writing tag by tag")
        status = {}
    for tag, value in items:
        if status.get(tag) != "Success":
            status[tag] = pylogix.writeinAb(plc, tag, value)
    return [status.get(tag, "Error") for tag, _ in items]


def _write_rockwell(p):
    plc = pylogix.connectABPLC(p["ip"])
    if plc is None:
        return {"success": False, "message": f"Unable to connect to the Rockwell PLC ({p['summary']['plc']})."}
    try:
        ready, _ = pylogix.readABPLC(plc, p["tagReadReady"]["Tag_name"], "BOOL")
        if ready is None:
            return {"success": False, "message": f"Could not reach the Rockwell PLC ({p['summary']['plc']})."}
        if not ready:
            return {"success": False, "message": "Rockwell PLC is not ready to receive a recipe (ReadyToReceive is off)."}

        tags = p["dfRecipeTags"].sort_values("Slot", kind="stable")
        tags["Status"] = _write_rockwell_bulk(plc, tags)
        header = p["dfHeader"]
        header["Status"] = _write_rockwell_bulk(plc, header)

        failed = _failed_tags(tags, "Success") + _failed_tags(header, "Success")
        if failed:
            return {"success": False, "message": f"Recipe NOT downloaded - PLC write failed for: {failed}"}

        if not pylogix.set_tag_ab(plc, p["tagWriteDwn"]["Tag_name"]):
            return {"success": False, "message": "Recipe values written but the RecipeDownloaded tag could not be set"}
        return None
    finally:
        try:
            plc.Close()
        except Exception:
            pass


def writePlcRecipe(mixerno, recipe_name, selected_module):
    """selected_module: 1 = Siemens, 2 = Rockwell (the driver chosen in Settings)."""
    try:
        p = prepare_download(mixerno, recipe_name, selected_module)
    except (DownloadError, ValueError) as e:
        logging.warning(f"Recipe download refused: {e}")
        return {"success": False, "message": str(e)}

    s = p["summary"]
    started = time.monotonic()
    try:
        error = _write_siemens(p) if p["driver"] == DRIVER_SIEMENS else _write_rockwell(p)
    except Exception as e:
        logging.exception("Recipe download failed")
        return {"success": False, "message": f"Recipe download failed: {e}"}

    if error:
        logging.error(f"Recipe '{recipe_name}' to mixer {s['mixer']}: {error['message']}")
        return error

    msg = (f"Recipe '{recipe_name}' downloaded to Mixer {s['mixer']} "
           f"({len(s['steps'])} steps, {s['driver_name']} {s['plc']})")
    logging.info(f"{msg} - {len(p['dfRecipeTags']) + len(p['dfHeader'])} tags "
                 f"in {time.monotonic() - started:.1f} s")
    return {"success": True, "message": msg}
