import pandas as pd
from sqlalchemy import text
from config import sqliteCon
from plc_connection import snap7_plc,pylogix
import logging
def writePlcRecipe(mixerno, recipe_name, selected_module):
    try:

        if selected_module == 1:
 
            engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()
            query = text('SELECT * FROM "RecipeTagName"')
            dfTags = pd.read_sql_query(query, engineConRead)
            
            # Separate Header/Control tags and Recipe tags
            dfHeader = dfTags[dfTags["SiloNo"].astype(str).str.isalpha()].copy()
            dfTags = dfTags[~dfTags["SiloNo"].astype(str).str.isalpha()].reset_index(drop=True)
            # ---------------------------------------------------------
            # Get Ready and Download Trigger Tags
            # ---------------------------------------------------------
            tagReadReady = dfHeader[dfHeader["SiloNo"] == "Read"]
            tagWriteDwn = dfHeader[dfHeader["SiloNo"] == "Write"]
            # Keep only actual header tags
            dfHeader = dfHeader[~dfHeader["SiloNo"].isin(["Read", "Write"])].reset_index(drop=True)
            if tagReadReady.empty:
                return {"success": False,"message": "ReadyToReciveRecipe tag not configured."}
            if tagWriteDwn.empty:
                return {"success": False,"message": "RecipeDownloaded tag not configured."}
            # Convert DataFrame -> Series
            tagReadReady = tagReadReady.iloc[0]
            tagWriteDwn = tagWriteDwn.iloc[0]
            # ---------------------------------------------------------
            # Validate Recipe Name
            # ---------------------------------------------------------
            if not recipe_name:
                return {"success": False,"message": "Recipe name not selected."}

            # ---------------------------------------------------------
            # Read Recipe Data
            # ---------------------------------------------------------
            # "recipeData" and "Category" are mixed-case identifiers and
            # must be quoted, or Postgres folds them to lowercase
            # (recipedata / category) and the query silently matches
            # nothing (or errors, if the lowercase names don't exist).
            query = text('SELECT * FROM "recipeData" WHERE "Category" = :category')
            dfRecipe = pd.read_sql_query(query,engineConRead,params={"category": recipe_name})
            
            if dfRecipe.empty:
                return {"success": False,"message": f"No recipe data found for '{recipe_name}'."}
            # --------------------------------------------------------
            # Merge PLC Tags with Recipe Values
            # --------------------------------------------------------
            dfRecipeTags = (dfTags.astype({"SiloNo": "int"}).merge(dfRecipe.astype({"SiloNo": "int"}),on="SiloNo",how="left"))
            dfRecipeTags["MaterialName"] = (dfRecipeTags["MaterialName"].fillna("."))
            dfRecipeTags[["SetWeight", "FineWeight", "Tolerance", "CoarseSpeed", "FineSpeed"]] = (dfRecipeTags[["SetWeight", "FineWeight", "Tolerance", "CoarseSpeed" ,"FineSpeed"]].fillna(0))
            
            # Create Value column
            dfRecipeTags["Value"] = (dfRecipeTags.apply(lambda row: row[row["Name"]],axis=1 ))
            
            # --------------------------------------------------------
            # Header Values
            # --------------------------------------------------------
            dfHeader.loc[dfHeader["Name"] == "PlantName","Value"] = "TEST"
            dfHeader.loc[dfHeader["Name"] == "RecipeName","Value"] = str(recipe_name)
            dfHeader.loc[dfHeader["Name"] == "MixerSelected","Value"] = str(mixerno)

            missing = dfHeader[dfHeader["Value"].isna()]["Name"].tolist()
            if missing:
                msg = f"Header tag(s) not found in RecipeTagName (renamed/removed?): {missing}"
                logging.error(msg)
                return {"success": False, "message": msg}
            # ---------------------------------------------------------
            # PLC Connection Info
            #  --------------------------------------------------------
            dfInfo = pd.read_sql_query('SELECT * FROM "Info_db";',engineConRead)
            if dfInfo.empty:
                return {"success": False,"message": "PLC configuration not found."}
            # NOTE: relies on row 0 of "Info_db" being the PLC connection
            # row. SQLite tended to preserve insertion order on a bare
            # SELECT *, but Postgres makes no such guarantee without an
            # explicit ORDER BY. Worth switching to
            # WHERE "Particulars" = '<key>' once you can confirm the key
            # name used for this row.
            node = dfInfo.loc[0, "Info"]
            plcIP, rack, slot = node.split(',')
            plc = snap7_plc.snap7Connect(plcIP,int(rack),int(slot))
            if plc is None:
                return {"success": False,"message": f"Unable to connect PLC ({plcIP})."}
            # ---------------------------------------------------------
            # Check PLC RUN State
            # ---------------------------------------------------------
            status = plc.get_cpu_state()
            print("PLC Status:", status)
            if status != "S7CpuStatusRun":
                try:
                    plc.disconnect()
                except:
                    pass



                return {"success": False,"message": f"PLC not in RUN mode. Current state: {status}" }
            # ---------------------------------------------------------
            # Check PLC Ready Bit
            # ---------------------------------------------------------
            ready, _ = snap7_plc.readSnap7PLC(plc,int(tagReadReady["db_number"]),int(float(tagReadReady["start_offset"])),tagReadReady["data_type"],
                    0 if pd.isna(tagReadReady["bit_offset"])else int(float(tagReadReady["bit_offset"])))
            print("PLC Ready Status :", ready)
            if not ready:
                msg = " Siemens PLC is Not Ready for Download Recipe "
                logging.warning(msg)
                try:
                    plc.disconnect()
                except:
                    pass
                return {"success": False,"message": msg}
            # ---------------------------------------------------------
            # Write Recipe Tags
            # ---------------------------------------------------------
            dfRecipeTags["Status"] = dfRecipeTags.apply(lambda row: snap7_plc.writeinSnap7(plc,int(row["db_number"]),int(float(row["start_offset"])),
                    0 if pd.isna(row["bit_offset"])else int(float(row["bit_offset"])),row["data_type"],row["Value"]),axis=1 )
            
            # ---------------------------------------------------------
            # Write Header Tags
            # ---------------------------------------------------------
            dfHeader["Status"] = dfHeader.apply(
                lambda row: snap7_plc.writeinSnap7(plc,int(row["db_number"]),int(float(row["start_offset"])),
                    0 if pd.isna(row["bit_offset"])else int(float(row["bit_offset"])),row["data_type"],row["Value"]),axis=1)
            
            # ---------------------------------------------------------
            # Trigger Download Bit
            # ---------------------------------------------------------
            
            response = snap7_plc.set_tag_snap7(plc,int(tagWriteDwn["db_number"]),int(float(tagWriteDwn["start_offset"])),0 if pd.isna(tagWriteDwn["bit_offset"])else int(float(tagWriteDwn["bit_offset"])))
           
            try:
                plc.disconnect()
            except:
                pass
            msg = "Recipes downloaded to PLC Successfully"
            logging.info(msg)
            return {"success": True,"message": msg}
        
        elif selected_module == 2:
            engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()
            query = text('SELECT * FROM "RecipeTagName"')
            dfTags = pd.read_sql_query(query, engineConRead)
            print(dfTags)
            # Split Header and Recipe Tags
            dfHeader = dfTags[dfTags["SiloNo"].astype(str).str.isalpha()].copy()
            dfTags = dfTags[~dfTags["SiloNo"].astype(str).str.isalpha()].reset_index(drop=True)
            print("dfHeader")
            print(dfHeader)
            print("dfTags")
            print(dfTags)
            # --------------------------------------------------
            # Get Ready and Download Trigger Tags
            # --------------------------------------------------
            tagReadReady = dfHeader[dfHeader["SiloNo"] == "Read"]["Name"]
            tagWriteDwn = dfHeader[dfHeader["SiloNo"] == "Write"]["Name"]
            dfHeader = dfHeader[~dfHeader["SiloNo"].isin(["Read", "Write"])].reset_index(drop=True)
            if tagReadReady.empty:
                return {"success": False,"message": "Ready tag not configured."}
            if tagWriteDwn.empty:
                return {"success": False,"message": "Download trigger tag not configured."}
            tagReadReady = tagReadReady.iloc[0]
            tagWriteDwn = tagWriteDwn.iloc[0]
            print("Ready Tag :", tagReadReady)
            print("Download Tag :", tagWriteDwn)
            # --------------------------------------------------
            # Validate Recipe Name
            # --------------------------------------------------
            if not recipe_name:
                return {"success": False,"message": "Recipe name not selected."}
            print("Recipe Name :", recipe_name)
            # --------------------------------------------------
            # Read Recipe Data
            # --------------------------------------------------
            # Same quoting fix as the selected_module == 1 branch above:
            # "recipeData" / "Category" are mixed-case and must be quoted.
            query = text('SELECT * FROM "recipeData" WHERE "Category" = :category')
            dfRecipe = pd.read_sql_query(query,engineConRead,params={"category": recipe_name})
            print(dfRecipe)
            if dfRecipe.empty:return {
                    "success": False,"message":f"No recipe data found for '{recipe_name}'."}
            # --------------------------------------------------
            # Merge PLC Tags with Recipe Data
            # --------------------------------------------------
            dfRecipeTags = (dfTags.astype({"SiloNo": "int"}).merge(dfRecipe.astype({"SiloNo": "int"}),on="SiloNo",how="left"))
            # Fill Missing Values
            dfRecipeTags["MaterialName"] = (dfRecipeTags["MaterialName"].fillna("."))
            dfRecipeTags[["SetWeight", "FineWeight", "Tolerance", "CoarseSpeed", "FineSpeed"]] = (dfRecipeTags[["SetWeight", "FineWeight", "Tolerance", "CoarseSpeed", "FineSpeed"]].fillna(0))
            # Create Value Column
            dfRecipeTags["Value"] = (dfRecipeTags.apply(lambda row: row[row["Name"]],axis=1))
            # Keep Required Columns
            # NOTE (pre-existing, not a DB-conversion issue): "Name" is
            # listed twice here. Left as-is since the intended third
            # column isn't clear from context — worth checking whether
            # this should be a different column (e.g. a tag identifier).
            dfRecipeTags = dfRecipeTags[["SiloNo", "Name", "Name", "Value"]]
            print("Final Recipe Tags")
            print(dfRecipeTags)
            # --------------------------------------------------
            # Header Values
            # --------------------------------------------------
            dfHeader.loc[dfHeader["Name"] == "PlantName","Value"] = "TEST"
            dfHeader.loc[dfHeader["Name"] == "RecipeName","Value"] = str(recipe_name)
            dfHeader.loc[dfHeader["Name"] == "MixerSelected","Value"] = int(mixerno)
            print("Header Tags")
            print(dfHeader)
            # --------------------------------------------------
            # PLC Connection Details
            # --------------------------------------------------
            dfInfo = pd.read_sql_query('SELECT * FROM "Info_db";',engineConRead)
            if dfInfo.empty:
                return {"success": False,"message": "PLC configuration not found."}
            # Same positional-row caveat noted in the selected_module == 1
            # branch above (row 0 of "Info_db" isn't guaranteed under
            # Postgres without an explicit ORDER BY).
            plcIP = dfInfo.loc[0, "Info"]
            print("PLC IP :", plcIP)
            plc = pylogix.connectABPLC(plcIP)
            if plc is None:
                return {"success": False,"message":f"Unable to connect PLC ({plcIP})"}
            # --------------------------------------------------
            # Check PLC Ready
            # --------------------------------------------------
            ready, _ = pylogix.readABPLC(plc,tagReadReady,"BOOL")
            print("PLC Ready :", ready)
            if not ready:
                msg = "Rockwell PLC is Not Ready for Download Recipe"
                logging.warning(msg)
                try:
                    plc.Close()
                except:
                    pass
                return {"success": False,"message": msg}
            # --------------------------------------------------
            # Write Recipe Tags
            # --------------------------------------------------
            print("Writing Recipe Tags...")
            dfRecipeTags["Status"] = (dfRecipeTags.apply(lambda row: pylogix.writeinAb(plc,row["Name"],row["Value"]),axis=1))
            print(dfRecipeTags)
            # --------------------------------------------------
            # Write Header Tags
            # --------------------------------------------------
            print("Writing Header Tags...")
            dfHeader["Status"] = (dfHeader.apply(lambda row: pylogix.writeinAb(plc,row["Name"],row["Value"]),axis=1))
            print(dfHeader)
            # --------------------------------------------------
            # Trigger Download Bit
            # --------------------------------------------------
            response = pylogix.set_tag_ab(plc,tagWriteDwn)
            print("Download Trigger Response:",response)
            try:
                plc.Close()
            except:
                pass
            msg = "Recipes downloaded to PLC Successfully"
            logging.info(msg)
            return {"success": True,"message": msg}
    
    except Exception as e:
        logging.exception(e)
        return {"success": False,"message": f"Recipe download failed: {str(e)}"}