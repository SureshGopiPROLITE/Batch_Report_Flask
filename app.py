from flask import (Flask, render_template, request, redirect, url_for, session, jsonify, abort, g, send_file, Response,)
from werkzeug.security import check_password_hash, generate_password_hash
import io
import logging
import json
import os
import tempfile
import subprocess
import threading
import webbrowser
from threading import Thread, Timer
from io import BytesIO
import numpy as np
import pandas as pd
import plotly
import psycopg2
import snap7
from datetime import datetime, date, timedelta
from sqlalchemy import text
# PLC Object
plc = snap7.client.Client()
# Modules
from auth import authLog, authMac, licence
from config import sqliteCon
from database import postgres

from modules import (monitor, main, Report, analytics_module, graphs, recipewrite, db_management,)
from modules.monitor import log_file
from modules.db_management import (
    get_database_management_data,
    record_backup_event,
)
app = Flask(__name__)
app.secret_key = os.environ.get(
    'SECRET_KEY', '4f3d6e9a5f4b1c8d7e6a2b3c9d0e8f1a5b7c2d4e6f9a1b3c8d0e6f2a9b1d3c4')


def open_browser():
    webbrowser.open("http://127.0.0.1:5000/")


@app.route('/')
def index():
    return redirect(url_for('home'))


@app.route('/home')
def home():
    if 'username' in session:
        return redirect(url_for('dashboard', user=session['username'], role=session.get('role')))
    else:
        return redirect(url_for('dashboard'))


@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')


@app.route("/api/dashboard", methods=["GET"])
def get_dashboard():
    try:
        start_time = request.args.get("start_time")
        end_time = request.args.get("end_time")
        hours = request.args.get("hours")
        print(f" Dashboard Filters → Hours: {hours}, Start: {start_time}, End: {end_time}")

        if hours and hours != "Custom":
            print("⚠ No valid start/end time provided, using hour-based filter")
            s_time = None
            e_time = None
        else:
            s_time = datetime.fromisoformat(start_time)
            e_time = datetime.fromisoformat(end_time)

        #  Fetch dashboard data
        data = main.dashboard_calculations(s_time, e_time, hours)

        #  No data found
        if not data:
            return jsonify({
                "status": "error",
                "message": "No data found for selected date range"
            }), 200

        #  Success response
        return jsonify({
            "status": "success",
            **data
        }), 200

    except ValueError:
        return jsonify({
            "status": "error",
            "message": "Invalid datetime format"
        }), 400

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


def get_available_years():

    engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()

    query = text("""
        SELECT DISTINCT
            EXTRACT(YEAR FROM "TimeStamp")::int AS year
        FROM "Batches"
        ORDER BY year DESC
    """)

    df = pd.read_sql(query, engineConRead)

    engineConRead.close()
    engineConWrite.close()

    return df["year"].tolist()


@app.route("/calendar_years")
def calendar_years():

    years = get_available_years()

    return jsonify(years)


@app.route("/calendar_data")
def calendar_data():

    year = request.args.get("year", type=int)

    if year is None:
        years = get_available_years()
        year = years[0] if years else datetime.now().year

    engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()

    try:
        query = text("""
            SELECT
                DATE("TimeStamp") AS date,
                COUNT(*) AS value
            FROM "Batches"
            WHERE EXTRACT(YEAR FROM "TimeStamp") = :year
            GROUP BY DATE("TimeStamp")
            ORDER BY DATE("TimeStamp")
        """)

        df = pd.read_sql(
            query,
            engineConRead,
            params={"year": year}
        )

        if not df.empty:
            df["date"] = df["date"].astype(str)

        return jsonify(df.to_dict(orient="records"))

    finally:
        engineConRead.close()
        engineConWrite.close()


@app.route('/logs')
def logs():
    return render_template('logs.html')


@app.route("/api/logs", methods=["GET"])
def get_logs():
    try:
        with open(log_file, "r") as f:
            lines = f.readlines()
        return jsonify({"logs": lines})
    except FileNotFoundError:
        return jsonify({"logs": []})


@app.route("/api/logs/clear", methods=["POST"])
def clear_logs():
    open(log_file, "w").close()
    return jsonify({"status": "cleared"})


@app.route('/recipe')
def recipe():
    user_logged_in = 'username' in session
    user_role = session.get("role")  # <-- Get role from session

    return render_template('recipe.html', user_logged_in=user_logged_in, role=user_role)


@app.route("/api/material/<silo_no>", methods=["GET"])
def get_material_by_silo(silo_no):
    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
    cursorRead.execute('SELECT "MaterialName" FROM "MaterialData" WHERE "SiloNo" = %s', (silo_no,))
    row = cursorRead.fetchone()
    conn.close()
    if row:
        return jsonify({"success": True, "MaterialName": row[0]})
    else:
        return jsonify({"success": False}), 404


@app.route("/api/recipes_data/get_recipes", methods=["GET"])
def get_recipes():
    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
    cursorRead.execute("SELECT id, name FROM recipes ORDER BY id ASC")
    data = [{"id": row[0], "name": row[1]} for row in cursorRead.fetchall()]
    conn.close()
    return jsonify(data)


@app.route("/api/recipes_data/add_recipe", methods=["POST"])
def add_recipe():
    data = request.json
    name = data.get("name")

    if not name or name.strip() == "":
        return jsonify({"success": False, "error": "Recipe name required"}), 400

    name = name.strip()

    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

    #  Check if recipe already exists
    cursorRead.execute("SELECT COUNT(*) FROM recipes WHERE name = %s", (name,))
    exists = cursorRead.fetchone()[0]

    if exists > 0:
        conn.close()
        return jsonify({"success": False, "error": "Recipe already exists"}), 409

    try:
        #  Insert into recipes table
        cursorWrite.execute(
            "INSERT INTO recipes (name, category) VALUES (%s, %s)",
            (name, name)
        )
        conn.commit()
        # No placeholder row: '' is not a valid number in Postgres (the insert
        # failed after the recipe was created). The page shows an empty table
        # with the Add button instead.
        return jsonify({"success": True})

    except Exception as e:
        print(" Error adding recipe:", e)
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        conn.close()


@app.route("/api/recipes_data/delete_recipe/<string:name>", methods=["DELETE"])
def delete_recipe(name):
    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
    cursorWrite.execute("DELETE FROM recipes WHERE name=%s", (name,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/recipes_data/rename_recipe", methods=["PUT"])
def rename_recipe():
    data = request.json
    old = data.get("old_name")
    new = data.get("new_name")

    # Validate
    if not old or not new:
        return jsonify({"success": False, "error": "old_name and new_name required"}), 400

    old = old.strip()
    new = new.strip()

    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

    #  Check if old recipe exists
    cursorRead.execute("SELECT COUNT(*) FROM recipes WHERE name = %s", (old,))
    old_exists = cursorRead.fetchone()[0]

    if old_exists == 0:
        conn.close()
        return jsonify({"success": False, "error": "Old recipe does not exist"}), 404

    #  Check if new recipe name already exists
    cursorRead.execute("SELECT COUNT(*) FROM recipes WHERE name = %s", (new,))
    new_exists = cursorRead.fetchone()[0]

    if new_exists > 0:
        conn.close()
        return jsonify({"success": False, "error": "New recipe name already exists"}), 409

    try:
        # Start rename
        cursorWrite.execute("UPDATE recipes SET name=%s WHERE name=%s", (new, old))
        cursorWrite.execute('UPDATE "recipeData" SET "Category"=%s WHERE "Category"=%s', (new, old))

        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        conn.rollback()
        print(" Rename error:", e)
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        conn.close()


@app.route("/api/recipes/<string:category>/table", methods=["GET"])
def get_recipe_table(category):
    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
    query = """
        SELECT r."Index", r."SiloNo",
            COALESCE(m."MaterialName", r."MaterialName") AS "MaterialName",
            r."SetWeight", r."FineWeight", r."Tolerance",
            COALESCE(r."InflightWeight", 0) AS "InflightWeight",
            r."CoarseSpeed", r."FineSpeed"
        FROM "recipeData" r
        LEFT JOIN "MaterialData" m ON r."SiloNo" = m."SiloNo"
        WHERE r."Category" = %s
        ORDER BY r."Seq" NULLS LAST, r."Index"
    """

    cursorRead.execute(query, (category,))
    data = cursorRead.fetchall()
    cols = [desc[0] for desc in cursorRead.description]
    conn.close()
    return jsonify([dict(zip(cols, row)) for row in data])


def normalize_recipe_seq(cursor, category):
    """Number a recipe's rows 1..n in their current order (rows saved before
    ordering existed have no Seq and keep their creation order)."""
    cursor.execute("""
        UPDATE "recipeData" r SET "Seq" = o.rn
        FROM (SELECT ctid, ROW_NUMBER() OVER (ORDER BY "Seq" NULLS LAST, "Index") AS rn
              FROM "recipeData" WHERE "Category" = %s) o
        WHERE r.ctid = o.ctid
    """, (category,))


@app.route("/api/recipes/<string:category>/order", methods=["PUT"])
def reorder_recipe(category):
    """Saves the step order: body {"order": [Index, Index, ...]} top to bottom.
    This is the order the recipe is written to the PLC."""
    if 'username' not in session or session.get('role') == 'operator':
        return jsonify({"success": False, "error": "Access denied"}), 403

    order = (request.get_json(silent=True) or {}).get("order") or []
    try:
        order = [int(i) for i in order]
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "order must be a list of row indexes"}), 400

    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
    try:
        cursorRead.execute('SELECT "Index" FROM "recipeData" WHERE "Category" = %s', (category,))
        existing = sorted(r[0] for r in cursorRead.fetchall())
        if sorted(order) != existing:
            return jsonify({"success": False, "error": "Recipe changed - reload the page and try again"}), 409

        for seq, index in enumerate(order, start=1):
            cursorWrite.execute('UPDATE "recipeData" SET "Seq" = %s WHERE "Index" = %s AND "Category" = %s',
                                (seq, index, category))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        print(" Reorder error:", e)
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/recipes_data/add_row", methods=["POST"])
def add_row():
    data = request.json
    silo = data.get("SiloNo")
    category = data.get("Category")

    # Validate inputs
    if not silo or not category:
        return jsonify({"success": False, "error": "SiloNo and Category required"}), 400

    silo = str(silo).strip()
    category = category.strip()

    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

    # 1 Check if Silo exists in MaterialData
    cursorRead.execute('SELECT "MaterialName" FROM "MaterialData" WHERE "SiloNo"=%s', (silo,))
    mrow = cursorRead.fetchone()

    if not mrow:
        conn.close()
        return jsonify({"success": False, "error": "silo_not_found"}), 404

    material_name = mrow[0]

    # 2 Check if SAME SiloNo already exists in recipeData under SAME Category
    cursorRead.execute("""
        SELECT COUNT(*)
        FROM "recipeData"
        WHERE "SiloNo" = %s AND "Category" = %s
    """, (silo, category))

    exists = cursorRead.fetchone()[0]

    if exists > 0:
        conn.close()
        return jsonify({"success": False, "error": "silo_already_exists"}), 409

    # 3 Insert new recipe row as the last step
    try:
        normalize_recipe_seq(cursorWrite, category)
        cursorWrite.execute("""
            INSERT INTO "recipeData"
                ("SiloNo", "MaterialName", "SetWeight", "FineWeight", "Tolerance", "InflightWeight",
                 "Category", "CoarseSpeed", "FineSpeed", "Seq")
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    (SELECT COALESCE(MAX("Seq"), 0) + 1 FROM "recipeData" WHERE "Category" = %s))
        """, (
            silo,
            material_name,
            data.get("SetWeight"),
            data.get("FineWeight"),
            data.get("Tolerance"),
            data.get("InflightWeight") or 0,
            category,
            data.get("CoarseSpeed"),
            data.get("FineSpeed"),
            category,
        ))

        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        conn.rollback()
        print(" Add Row Error:", e)
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        conn.close()


@app.route('/api/recipes/export', methods=['POST'])
def export_recipe_data():
    try:
        payload = request.get_json()
        category = payload.get("category")

        if not category:
            return jsonify({"success": False, "error": "No category provided"}), 400

        print(f" Export Recipe → {category}")

        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

        query = """
            SELECT
                r."SiloNo",
                COALESCE(m."MaterialName", r."MaterialName") AS "MaterialName",
                r."SetWeight",
                r."FineWeight",
                r."Tolerance",
                COALESCE(r."InflightWeight", 0) AS "InflightWeight",
                r."CoarseSpeed",
                r."FineSpeed"

            FROM "recipeData" r
            LEFT JOIN "MaterialData" m ON r."SiloNo" = m."SiloNo"
            WHERE r."Category" = %s
            ORDER BY r."Seq" NULLS LAST, r."Index"
        """

        df = pd.read_sql_query(query, conn, params=(category,))

        if df.empty:
            return jsonify({"success": False, "error": "No data found for this recipe"}), 400

        # Create Excel in memory
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name=category[:31])

        output.seek(0)

        filename = f"{category}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    except Exception as e:
        print(" Export Recipe Error:", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/recipes/import", methods=["POST"])
def import_recipe_excel():
    try:
        if "file" not in request.files:
            return jsonify({"success": False, "error": "No file uploaded"}), 400

        file = request.files["file"]
        filename = file.filename

        if not filename.endswith(".xlsx"):
            return jsonify({"success": False, "error": "Only .xlsx allowed"}), 400

        # Category = file name without extension
        category = filename.rsplit(".", 1)[0].strip()

        # Load Excel into pandas
        df = pd.read_excel(file)

        required_cols = ["SiloNo", "MaterialName", "SetWeight", "FineWeight", "Tolerance", "CoarseSpeed", "FineSpeed"]

        for col in required_cols:
            if col not in df.columns:
                return jsonify({"success": False, "error": f"Missing column: {col}"}), 400
        # Optional: recipe files exported before this column existed
        if "InflightWeight" not in df.columns:
            df["InflightWeight"] = 0
        df["InflightWeight"] = pd.to_numeric(df["InflightWeight"], errors="coerce").fillna(0)

        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

        # 1 Check if recipe already exists
        cursorRead.execute("SELECT COUNT(*) FROM recipes WHERE name = %s", (category,))
        if cursorRead.fetchone()[0] > 0:
            return jsonify({"success": False, "error": "Recipe already exists"}), 409

        # 2 Insert recipe name
        cursorWrite.execute(
            "INSERT INTO recipes (name, category) VALUES (%s, %s)",
            (category, category)
        )
        conn.commit()

        # 3 Insert all rows into recipeData, in the Excel row order
        for seq, (_, row) in enumerate(df.iterrows(), start=1):
            silo = str(row["SiloNo"]).strip()

            # Validate silo exists in MaterialData
            cursorRead.execute('SELECT "MaterialName" FROM "MaterialData" WHERE "SiloNo"=%s', (silo,))
            mr = cursorRead.fetchone()

            if not mr:
                conn.rollback()
                return jsonify({"success": False, "error": f"Silo not found: {silo}"}), 400

            cursorWrite.execute("""
                INSERT INTO "recipeData"
                    ("SiloNo", "MaterialName", "SetWeight", "FineWeight", "Tolerance", "InflightWeight",
                     "CoarseSpeed", "FineSpeed", "Category", "Seq")
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                silo,
                mr[0],                              # MaterialName from MaterialData
                row["SetWeight"],
                row["FineWeight"],
                row["Tolerance"],
                float(row["InflightWeight"]),
                row["CoarseSpeed"],
                row["FineSpeed"],
                category,
                seq
            ))

        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        print(" Import Error:", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/recipes_data/update_row/<int:index>", methods=["PUT"])
def update_row(index):
    data = request.json

    silo = data.get("SiloNo")
    category = data.get("Category")
    set_weight = data.get("SetWeight")
    fine_weight = data.get("FineWeight")
    tolerance = data.get("Tolerance")
    inflight = data.get("InflightWeight") or 0
    CoarseSpeed = data.get("CoarseSpeed")
    FineSpeed = data.get("FineSpeed")

    # Validate required fields
    if not silo or not category:
        return jsonify({"success": False, "error": "SiloNo and Category required"}), 400

    silo = str(silo).strip()
    category = category.strip()

    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

    # 1 Check if Silo exists in MaterialData
    cursorRead.execute('SELECT "MaterialName" FROM "MaterialData" WHERE "SiloNo" = %s', (silo,))
    mrow = cursorRead.fetchone()

    if not mrow:
        conn.close()
        return jsonify({"success": False, "error": "silo_not_found"}), 404

    material_name = mrow[0]

    # 2 Check that Index exists in recipeData
    cursorRead.execute('SELECT COUNT(*) FROM "recipeData" WHERE "Index"=%s', (index,))
    if cursorRead.fetchone()[0] == 0:
        conn.close()
        return jsonify({"success": False, "error": "row_not_found"}), 404

    # 3 Duplicate Silo check (same recipe & category)
    cursorRead.execute("""
        SELECT COUNT(*) FROM "recipeData"
        WHERE "SiloNo" = %s
          AND "Category" = %s
          AND "Index" != %s
    """, (silo, category, index))

    if cursorRead.fetchone()[0] > 0:
        conn.close()
        return jsonify({"success": False, "error": "silo_already_exists"}), 409

    # 4 Perform the UPDATE
    try:
        cursorWrite.execute("""
            UPDATE "recipeData"
            SET "SiloNo" = %s,
                "Category" = %s,
                "MaterialName" = %s,
                "SetWeight" = %s,
                "FineWeight" = %s,
                "Tolerance" = %s,
                "InflightWeight" = %s,
                "CoarseSpeed" = %s,
                "FineSpeed" = %s
            WHERE "Index" = %s
        """, (
            silo,
            category,
            material_name,
            set_weight,
            fine_weight,
            tolerance,
            inflight,
            CoarseSpeed,
            FineSpeed,
            index
        ))

        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        conn.rollback()
        print(" Update row error:", e)
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        conn.close()


@app.route("/api/recipes_data/delete_row/<int:index>", methods=["DELETE"])
def delete_row(index):
    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

    # "Index" is a stable row id (database sequence). It used to be renumbered
    # 1..N for every recipe here, which changed the ids of rows other open
    # pages were still showing; the step order lives in "Seq" instead.
    cursorWrite.execute('DELETE FROM "recipeData" WHERE "Index"=%s', (index,))
    conn.commit()
    conn.close()

    return jsonify({"success": True})


@app.route('/report')
def report():
    user_logged_in = 'username' in session
    username = session.get("username")
    user_role = session.get("role")

    return render_template('report.html', user_logged_in=user_logged_in, username=username, role=user_role)


def json_safe(obj):
    """Recursively convert objects into JSON-serializable values."""

    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]

    # Handle datetime objects
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return obj.isoformat(sep=" ") if hasattr(obj, "hour") else obj.isoformat()

    # NumPy integer
    if isinstance(obj, np.integer):
        return int(obj)

    # NumPy float
    if isinstance(obj, np.floating):
        return None if np.isnan(obj) else float(obj)

    # NumPy bool
    if isinstance(obj, np.bool_):
        return bool(obj)

    # NaN / NaT
    if pd.isna(obj):
        return None

    return obj


@app.route('/api/report_data', methods=['POST'])
def api_report_data():
    try:
        payload = request.get_json(force=True) or {}

        hours = payload.get("hours")
        from_time = payload.get("from_time")
        to_time = payload.get("to_time")

        print(
            f" Received Filters → Hours: {hours}, "
            f"From: {from_time}, To: {to_time}"
        )

        result = main.data_process(hours, from_time, to_time)

        return jsonify({
            "success": bool(result.get("success", False)),
            "data": json_safe(result.get("data", [])),
            "total_weight": float(result.get("total_weight", 0.0))
        })

    except Exception as e:
        print(f" API Error: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ===============================================================
#  FETCH PLC DATA FOR POPUP
# ===============================================================
@app.route('/api/plc_data', methods=['POST'])
def api_plc_data():
    try:
        batch_no = request.json.get('batch_no')
        print("IN", batch_no)
        if not batch_no:
            return jsonify({"success": False, "error": "Missing BatchNo"}), 400

        df_pivot, df_string, daily_batch_no, df_cal_sum = main.report_data_process(batch_no)
        print(df_pivot)
        if isinstance(df_pivot, dict) and not df_pivot.get("success", True):
            return jsonify(df_pivot)

        data = df_pivot.to_dict(orient="records")

        # Batch header for the popup: Info tags (Plant/Recipe/Mixer/Start/End)
        # and the Summary rows (totals, accuracy, batch time)
        details = {}
        logged_at = None
        for frame in (df_string, df_cal_sum):
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                details.update(zip(frame["Name"], frame["Value"]))
                if logged_at is None and "TimeStamp" in frame.columns:
                    logged_at = frame["TimeStamp"].min()
        details["LoggedAt"] = str(logged_at)[:19] if logged_at is not None else ""

        try:
            daily_batch = int(daily_batch_no)
        except (TypeError, ValueError):
            daily_batch = None

        return jsonify({"success": True, "data": json_safe(data), "daily_batch": daily_batch,
                        "details": json_safe(details)})
    except Exception as e:
        print(f" API Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def get_column_settings():
    """
    Returns True if CoarseSpeed and FineSpeed
    should be displayed in the report.
    Reads the persisted value from Info_DB.
    """
    try:
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        cursorRead.execute(
            'SELECT "Info" FROM "Info_db" WHERE "Particulars" = \'ShowSpeedColumns\''
        )
        row = cursorRead.fetchone()
        conn.close()

        if row is None:
            return True  # default: show columns until a user changes it

        return str(row[0]) == "1"
    except Exception as e:
        print(" get_column_settings error:", e)
        return True


# ===============================================================
#  PDF REPORT DOWNLOAD
# ===============================================================
@app.route('/api/plc_data/pdf', methods=['POST'])
def api_plc_data_pdf():
    try:
        batch_no = request.json.get('batch_no')
        if not batch_no:
            return jsonify({"success": False, "error": "BatchNo missing"}), 400

        df_pivot, df_string, daily_batch_no, df_cal_sum = main.report_data_process(batch_no)
        show_speed = get_column_settings()

        pdf_bytes = Report.generate_pdf_report(
            df_pivot,
            df_string,
            batch_no,
            df_cal_sum,
            include_speed=show_speed
        )

        return send_file(
            io.BytesIO(pdf_bytes),
            as_attachment=True,
            download_name=f"BatchReport_{batch_no}.pdf",
            mimetype="application/pdf"
        )
    except Exception as e:
        print(f" PDF Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ===============================================================
#  EXCEL REPORT DOWNLOAD
# ===============================================================
@app.route('/api/plc_data/excel', methods=['POST'])
def api_plc_data_excel():
    try:
        batch_no = request.json.get('batch_no')
        if not batch_no:
            return jsonify({"success": False, "error": "BatchNo missing"}), 400

        df_pivot, df_string, daily_batch_no, df_cal_sum = main.report_data_process(batch_no)
        show_speed = get_column_settings()

        excel_bytes = Report.generate_excel_report(
            df_pivot,
            df_string,
            batch_no,
            df_cal_sum,
            include_speed=show_speed
        )

        return send_file(
            io.BytesIO(excel_bytes),
            as_attachment=True,
            download_name=f"BatchReport_{batch_no}.xlsx",
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
    except Exception as e:
        print(f" Excel Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# Report / Export column order (same as the report table)
REPORT_EXPORT_COLUMNS = ["BatchNo", "TimeStamp", "Shift", "Plant Name", "Recipe Name", "Mixer No",
                         "Start Date Time", "End Date Time",
                         "Total Set Weight(Kg)", "Total Actual Weight(Kg)"]


MULTI_REPORT_LIMIT = {"pdf": 100, "excel": 500}


@app.route('/api/plc_data/multi', methods=['POST'])
def api_plc_data_multi():
    """Batch reports for the rows ticked in the report table.
    body: {"batch_nos": [...], "type": "pdf" | "excel"}
    pdf   -> one PDF, each batch report on its own page
    excel -> one workbook: Summary sheet + one sheet per batch"""
    try:
        payload = request.get_json(silent=True) or {}
        kind = payload.get("type")
        if kind not in MULTI_REPORT_LIMIT:
            return jsonify({"success": False, "error": "type must be pdf or excel"}), 400
        try:
            batch_nos = sorted({int(b) for b in payload.get("batch_nos") or []})
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "batch_nos must be numbers"}), 400
        if not batch_nos:
            return jsonify({"success": False, "error": "Select at least one batch"}), 400
        limit = MULTI_REPORT_LIMIT[kind]
        if len(batch_nos) > limit:
            return jsonify({"success": False,
                            "error": f"{len(batch_nos)} batches selected - {kind.upper()} export allows up to {limit} at a time"}), 400

        batches, summary = [], []
        for no in batch_nos:
            df_pivot, df_string, daily_batch_no, df_cal_sum = main.report_data_process(no)
            if not isinstance(df_pivot, pd.DataFrame) or df_pivot.empty:
                continue
            batches.append((no, df_pivot, df_string, df_cal_sum))
            info = dict(zip(df_string["Name"], df_string["Value"])) if not df_string.empty else {}
            cal = dict(zip(df_cal_sum["Name"], df_cal_sum["Value"])) if not df_cal_sum.empty else {}
            set_total = pd.to_numeric(pd.Series([cal.get("TotalBatchSetWeight")]), errors="coerce").iloc[0]
            act_total = pd.to_numeric(pd.Series([cal.get("TotalBatchActualWeight")]), errors="coerce").iloc[0]
            if pd.isna(set_total):
                set_total = pd.to_numeric(df_pivot.get("SetWeight"), errors="coerce").sum()
            if pd.isna(act_total):
                act_total = pd.to_numeric(df_pivot.get("ActualWeight"), errors="coerce").sum()
            summary.append({
                "BatchNo": no, "Daily Batch No": daily_batch_no, "Shift": info.get("Shift", ""),
                "Plant Name": info.get("Plant Name", ""), "Recipe Name": info.get("Recipe Name", ""),
                "Mixer No": info.get("Mixer Selected", ""),
                "Start Date Time": info.get("Start Date Time", ""), "End Date Time": info.get("End Date Time", ""),
                "Silos Used": len(df_pivot),
                "Total Set Weight(Kg)": round(float(set_total), 2),
                "Total Actual Weight(Kg)": round(float(act_total), 2),
            })
        if not batches:
            return jsonify({"success": False, "error": "No PLC data found for the selected batches"}), 404

        show_speed = get_column_settings()
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        if kind == "pdf":
            data = Report.generate_multi_pdf_report(batches, include_speed=show_speed)
            return send_file(io.BytesIO(data), as_attachment=True, mimetype="application/pdf",
                             download_name=f"BatchReports_{len(batches)}_{stamp}.pdf")

        summary_df = pd.DataFrame(summary)
        totals = {c: "" for c in summary_df.columns}
        totals["BatchNo"] = f"Total ({len(summary_df)})"
        for c in ("Total Set Weight(Kg)", "Total Actual Weight(Kg)"):
            totals[c] = round(summary_df[c].sum(), 2)
        summary_df = pd.concat([summary_df, pd.DataFrame([totals])], ignore_index=True)
        data = Report.generate_multi_excel_report(batches, summary_df, include_speed=show_speed)
        return send_file(io.BytesIO(data), as_attachment=True,
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                         download_name=f"BatchReports_{len(batches)}_{stamp}.xlsx")

    except Exception as e:
        logging.exception("Multi batch report failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/export_data', methods=['POST'])
def api_export_data():
    """Excel of the report rows. With "batch_nos" (the rows left after the
    search box filter) only those batches are exported."""
    try:
        payload = request.get_json(silent=True) or {}
        hours = payload.get('hours')
        from_time = payload.get('from_time')
        to_time = payload.get('to_time')
        batch_nos = payload.get('batch_nos')
        search = (payload.get('search') or "").strip()

        result = main.data_process(hours, from_time, to_time)
        df = pd.DataFrame(result.get("data") or [])
        if df.empty:
            return jsonify({"success": False, "error": "No data available to export"}), 400

        if batch_nos is not None:
            wanted = {int(b) for b in batch_nos if str(b).strip().lstrip("-").isdigit()}
            df = df[df["BatchNo"].astype(int).isin(wanted)]
            if df.empty:
                return jsonify({"success": False, "error": "No rows match the search"}), 400

        cols = [c for c in REPORT_EXPORT_COLUMNS if c in df.columns]
        cols += [c for c in df.columns if c not in cols]
        df = df[cols]

        totals = {c: "" for c in cols}
        totals[cols[0]] = f"Total ({len(df)} batches)"
        for c in ("Total Set Weight(Kg)", "Total Actual Weight(Kg)"):
            if c in df.columns:
                totals[c] = round(pd.to_numeric(df[c], errors="coerce").sum(), 2)

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            info = [f"Range: {from_time or ''} to {to_time or ''}" if hours == "Custom" else f"Range: last {hours}"]
            if search:
                info.append(f'Filter: "{search}"')
            df.to_excel(writer, index=False, sheet_name='ReportData', startrow=2)
            ws = writer.sheets['ReportData']
            bold = writer.book.add_format({"bold": True})
            ws.write(0, 0, "  |  ".join(info), bold)
            last = len(df) + 3
            for i, c in enumerate(cols):
                ws.write(last, i, totals[c], bold)
                width = max([len(str(c))] + [len(str(v)) for v in df[c].head(500)]) + 2
                ws.set_column(i, i, min(width, 40))
            ws.freeze_panes(3, 0)
            ws.autofilter(2, 0, len(df) + 2, len(cols) - 1)
        output.seek(0)

        filename = f"Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    except Exception as e:
        logging.exception("Export failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/settings/shift_times', methods=['GET', 'POST'])
def shift_times():
    """Shift start times, e.g. "A=06:00,B=14:00,C=22:00" (Info_db Shift_Times)."""
    from modules import shift as shift_mod
    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
    try:
        if request.method == 'GET':
            return jsonify(success=True, shift_times=shift_mod.to_text(shift_mod.load(cursorRead)))

        if session.get('role') not in ('admin', 'superadmin'):
            return jsonify(success=False, message="Only an admin can change the shift times"), 403
        text_value = (request.get_json(silent=True) or {}).get("shift_times", "")
        try:
            shifts = shift_mod.parse(text_value)
        except ValueError as e:
            return jsonify(success=False, message=str(e)), 400
        value = shift_mod.to_text(shifts)
        cursorWrite.execute('UPDATE "Info_db" SET "Info" = %s WHERE "Particulars" = %s',
                            (value, shift_mod.INFO_KEY))
        if cursorWrite.rowcount == 0:
            cursorWrite.execute('INSERT INTO "Info_db" ("Id", "Particulars", "Info") '
                                'SELECT COALESCE(MAX("Id"), 0) + 1, %s, %s FROM "Info_db"',
                                (shift_mod.INFO_KEY, value))
        conn.commit()
        return jsonify(success=True, shift_times=value, message=f"Shift times saved: {value}")
    finally:
        conn.close()


@app.route('/analytics')
def analytics():

    return redirect(url_for('analytics_tab', tab='data'))


@app.route("/api/analytics_data", methods=["POST"])
def analytics_data():
    """
    Expects JSON: { "hours": "1 Hr" | "4 Hr" | ... | "Custom",
                   "from_time": "2025-11-01T10:00" (ISOLocal) optional,
                   "to_time": "2025-11-01T12:00" (ISOLocal) optional }
    Returns:
      {
        "success": True,
        "data": [ {Category:..., SetWeight:..., ActualWeight:..., Error_%:..., Error_Kg:...}, ... ],
        "total_weight": <float>  # total Error_Kg sum / 1000 (rounded)
      }
    """
    try:

        payload = request.get_json() or {}
        hours = payload.get("hours", "1 Hr")
        from_time = payload.get("from_time")
        to_time = payload.get("to_time")
        print(f" Analytics Filters → Hours: {hours}, From: {from_time}, To: {to_time}")
        # Basic validation for Custom range
        if hours == "Custom":
            if not from_time or not to_time:
                return jsonify({"success": False, "error": "Custom range requires from_time and to_time"}), 400
            try:
                datetime.fromisoformat(from_time)
                datetime.fromisoformat(to_time)
            except Exception:
                return jsonify({"success": False, "error": "Invalid from_time/to_time format, use ISO format"}), 400

        # Get DB connections (uses your existing helper)
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()

        # Use your existing show_data to fetch raw rows
        df = sqliteCon.show_data(conn, hours, from_time, to_time, engineConRead)

        if df is None or df.empty:
            return jsonify({"success": True, "data": [], "total_weight": 0.0})

        # Use your existing processing function
        df_diff = sqliteCon.process_batch_data(df)
        if df_diff is None or df_diff.empty:
            return jsonify({"success": True, "data": [], "total_weight": 0.0})

        # Keep only the columns your frontend expects (if present)
        column_order = ["Category", "SetWeight", "ActualWeight", "Error_%", "Error_Kg"]
        existing_columns = [c for c in column_order if c in df_diff.columns]
        df_diff = df_diff[existing_columns]

        # Calculate total (sum Error_Kg -> convert to tons by dividing 1000)
        total_error_kg = df_diff["Error_Kg"].sum() if "Error_Kg" in df_diff.columns else 0
        total_tons = round(total_error_kg / 1000.0, 2)
        df_diff["ActualWeight"] = df_diff["ActualWeight"].apply(lambda x: f"{float(x):.2f}")

        # Convert to JSON-serializable structure
        data = df_diff.fillna("").to_dict(orient="records")

        return jsonify({"success": True, "data": data, "total_weight": total_tons})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/export_data_analytics", methods=["POST"])
def export_data_analytics():
    """
    Exports processed analytics data as an Excel file.
    Expects JSON body: { "hours": "1 Hr", "from_time": "...", "to_time": "..." }
    """
    try:
        payload = request.get_json() or {}
        hours = payload.get("hours", "1 Hr")
        from_time = payload.get("from_time")
        to_time = payload.get("to_time")

        # Validation for custom range
        if hours == "Custom" and (not from_time or not to_time):
            return jsonify({"success": False, "error": "Missing from/to times"}), 400

        # DB Connection
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()

        # Fetch raw data
        df = sqliteCon.show_data(conn, hours, from_time, to_time, engineConRead)
        if df is None or df.empty:
            return jsonify({"success": False, "error": "No data available for export"}), 404

        # Process
        df_diff = sqliteCon.process_batch_data(df)
        if df_diff is None or df_diff.empty:
            return jsonify({"success": False, "error": "No processed data to export"}), 404

        # Reorder columns
        column_order = ["Category", "SetWeight", "ActualWeight", "Error_%", "Error_Kg"]
        existing_columns = [col for col in column_order if col in df_diff.columns]
        df_diff = df_diff[existing_columns]

        # Convert to Excel in memory
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            df_diff.to_excel(writer, index=False, sheet_name="Analytics_Report")
            worksheet = writer.sheets["Analytics_Report"]
            # Optional: auto adjust column widths
            for i, col in enumerate(df_diff.columns):
                max_len = max(df_diff[col].astype(str).map(len).max(), len(col))
                worksheet.set_column(i, i, max_len + 3)

        output.seek(0)

        # Generate filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"Analytics_Report_{timestamp}.xlsx"

        # Send as file download
        return send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        print(f" Export error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/plc_data_analytics", methods=["POST"])
def plc_data_analytics():
    try:
        data = request.get_json() or {}

        #  Extract parameters safely
        category = data.get("category")  # Category or Silo
        hours = data.get("hours", "1 Hr")
        from_time = data.get("from_time")
        to_time = data.get("to_time")

        #  Validation
        if not category:
            return jsonify({"success": False, "error": "Missing 'category' field"}), 400

        #  Get DB connections
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()

        #  Fetch main PLC data
        df = sqliteCon.show_data(conn, hours, from_time, to_time, engineConRead)
        if df is None or df.empty:
            return jsonify({"success": False, "error": "No PLC data found for the selected range"}), 404

        # Filter and transform data
        df = df[df["Category"] != "Info"]
        df_pivot = sqliteCon.get_silo_pivot(df, category)
        if df_pivot is None or df_pivot.empty:
            return jsonify({"success": True, "data": []})

        #  Fixed column order
        col_order = [
            "Category", "SetWeight", "ActualWeight", "FineWeight",
            "Error_Kg", "Error_%", "DiffPerc", "DiffKg", "TimeStamp"
        ]
        existing_cols = [c for c in col_order if c in df_pivot.columns]
        df_pivot = df_pivot[existing_cols]

        #  Send cleaned response
        return jsonify({
            "success": True,
            "data": df_pivot.fillna("").to_dict(orient="records")
        })

    except Exception as e:
        print(f" Error in /api/plc_data_analytics: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/plc_data_analytics_excel", methods=["POST"])
def plc_data_analytics_EXCEL():
    try:
        data = request.get_json() or {}

        # Extract parameters
        category = data.get("category")
        hours = data.get("hours", "1 Hr")
        from_time = data.get("from_time")
        to_time = data.get("to_time")

        if not category:
            return jsonify({"success": False, "error": "Missing 'category' field"}), 400

        # DB connections
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()

        # Fetch PLC data
        df = sqliteCon.show_data(conn, hours, from_time, to_time, engineConRead)
        if df is None or df.empty:
            return jsonify({"success": False, "error": "No PLC data found"}), 404

        # Filter & pivot
        df = df[df["Category"] != "Info"]
        df_pivot = sqliteCon.get_silo_pivot(df, category)

        if df_pivot is None or df_pivot.empty:
            return jsonify({"success": False, "error": "No data available"}), 404

        # Column order
        col_order = [
            "Category", "SetWeight", "ActualWeight", "FineWeight",
            "Error_Kg", "Error_%", "DiffPerc", "DiffKg", "TimeStamp"
        ]
        df_pivot = df_pivot[[c for c in col_order if c in df_pivot.columns]]

        # Convert to Excel in memory
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df_pivot.to_excel(writer, index=False, sheet_name=f"{category}")

        output.seek(0)

        filename = f"PLC_Data_{category}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    except Exception as e:
        print(f" Error in /api/plc_data_analytics_EXCEL: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/analytics/dash", methods=["GET"])
def analytics_dashboard():
    try:
        print(" Starting Dashboard...")

        dashboard_thread = threading.Thread(
            target=analytics_module.run_dashboard,
            daemon=True
        )
        dashboard_thread.start()
        server_host = request.host.split(":")[0]
        dash_url = f"http://{server_host}:8050"

        print(f" Dashboard URL: {dash_url}")

        return jsonify({
            "success": True,
            "url": dash_url
        })

    except Exception as e:
        print(" Dashboard failed:", e)
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/analytics/graph/data', methods=['GET', 'POST'])
def get_analytics_graph_data():
    try:
        print(" Flask route reached /api/analytics/graph/data")
        # Safe JSON extraction
        data = request.get_json(force=True, silent=True) or {}
        print(" Incoming JSON:", data)

        hours = data.get("hours", "1 Hr")
        from_time = data.get("from_time")
        to_time = data.get("to_time")

        print(f" Received parameters → Hours: {hours}, From: {from_time}, To: {to_time}")

        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()

        df = sqliteCon.show_data(conn, hours, from_time, to_time, engineConRead)

        if df is None or df.empty:
            return jsonify({"error": "No data found"})

        df_diff = sqliteCon.process_batch_data(df)

        if df_diff is None or df_diff.empty:
            return jsonify({"error": "No data found"})

        total_error_kg = round(df_diff["Error_Kg"].sum(), 2)
        total_error_per = round(df_diff["Error_%"].mean(), 2)

        return jsonify({
            "data": df_diff.to_dict(orient="records"),
            "total_error_kg": total_error_kg,
            "total_error_per": total_error_per
        })
    except Exception as e:
        print(" Graph API error:", e)
        return jsonify({"error": "No data found"})


@app.route('/analytics/<tab>')
def analytics_tab(tab):
    user_logged_in = 'username' in session
    username = session.get("username")
    user_role = session.get("role")
    if tab == 'graph':
        return render_template('analyticsgraph.html', tab='graph', user_logged_in=user_logged_in, username=username, role=user_role)
    return render_template('analyticsdata.html', tab='data', user_logged_in=user_logged_in, username=username, role=user_role)


# ----------------------------------
# settings page
# ----------------------------------
@app.route('/settings')
def settings():
    user_logged_in = 'username' in session
    user_role = session.get("role")
    db_data = get_database_management_data()

    return render_template(
        "settingsADV.html",
        user_logged_in=user_logged_in,
        role=user_role,
        db_data=db_data
    )


@app.route("/api/settings/update_email", methods=["POST"])
def update_email():
    try:
        email = request.form.get("email")

        if not email:
            return jsonify({
                "success": False,
                "error": "Email ID is required"
            })

        # Save email to database/config/file
        print("Email:", email)

        return jsonify({
            "success": True
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        })


@app.route("/api/settings/save_column_settings", methods=["POST"])
def save_column_settings():
    try:
        data = request.get_json() or {}
        show = data.get("show_cspeed_fspeed", True)

        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

        cursorRead.execute(
            'SELECT COUNT(*) FROM "Info_db" WHERE "Particulars" = \'ShowSpeedColumns\''
        )
        exists = cursorRead.fetchone()[0]

        if exists:
            cursorWrite.execute(
                'UPDATE "Info_db" SET "Info" = %s WHERE "Particulars" = \'ShowSpeedColumns\'',
                ("1" if show else "0",)
            )
        else:
            cursorWrite.execute(
                'INSERT INTO "Info_db" ("Particulars", "Info") VALUES (%s, %s)',
                ("ShowSpeedColumns", "1" if show else "0")
            )

        conn.commit()
        conn.close()
        return jsonify({"success": True})

    except Exception as e:
        print(" save_column_settings error:", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/settings/get_column_settings", methods=["GET"])
def api_get_column_settings():
    return jsonify({"success": True, "show_cspeed_fspeed": get_column_settings()})


@app.route("/api/settings/update_report", methods=["POST"])
def update_report():
    try:
        report_name = request.form.get("report_name")

        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

        # 1 - Update report name
        if report_name and report_name.strip():
            cursorWrite.execute(
                'UPDATE "Info_db" SET "Info" = %s WHERE "Particulars" = \'Company_Name\'',
                (report_name,)
            )
            conn.commit()

        # 2 - Save uploaded logo into data_files/
        if "logo" in request.files:
            logo = request.files["logo"]

            if logo.filename != "":
                # Ensure directory exists
                save_dir = os.path.join("data_files")
                os.makedirs(save_dir, exist_ok=True)

                # Final save path
                save_path = os.path.join(save_dir, "logo.png")
                logo.save(save_path)

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/download-RecipeTag', methods=['GET'])
def download_RecipeTag():
    try:
        # Get database connection
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        engine, engineConRead, engineConWrite = sqliteCon.get_db_connection_engine()

        # Read table into DataFrame
        df = pd.read_sql_query(
            'SELECT * FROM "RecipeTagName"',
            engine
        )

        # Create Excel file in memory
        output = io.BytesIO()

        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='RecipeTagName')

        output.seek(0)

        return send_file(
            output,
            as_attachment=True,
            download_name='RecipeTagName.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    except Exception as e:
        return {"error": str(e)}, 500


# Columns each driver needs in the uploaded tag tables (Settings -> Configuration)
PLC_TAG_COLUMNS = {
    monitor.DRIVER_SIEMENS: ["Name", "Category", "db_number", "start_offset", "data_type"],
    monitor.DRIVER_ROCKWELL: ["Name", "Category", "Tag_name", "Data_type"],
}


def tag_table_error(df, required, what):
    """Message if an uploaded tag table does not suit the driver chosen in Settings."""
    driver = monitor.get_saved_driver()
    missing = [c for c in required[driver] if c not in df.columns]
    if not missing:
        return None
    return (f"This {what} file is not a {monitor.DRIVER_NAMES[driver]} tag table "
            f"(missing column {', '.join(missing)}). Settings driver is "
            f"{monitor.DRIVER_NAMES[driver]} - choose the right driver first, or upload "
            f"the {monitor.DRIVER_NAMES[driver]} file.")


@app.route('/upload-RecipeTag', methods=['POST'])
def upload_RecipeTag():
    try:
        if 'file' not in request.files:
            return jsonify({
                "success": False,
                "message": "No file uploaded"
            }), 400

        file = request.files['file']

        if file.filename == '':
            return jsonify({
                "success": False,
                "message": "No file selected"
            }), 400

        # Read Excel file
        dfPlcExcel = pd.read_excel(file)

        required = {d: ["Name", "SiloNo"] + cols for d, cols in recipewrite.TAG_COLUMNS.items()}
        error = tag_table_error(dfPlcExcel, required, "recipe tag")
        if error:
            return jsonify({"success": False, "message": error}), 400

        # Insert into Postgres
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

        postgres.insert_data_into_sqlite_rec(
            cursorWrite,
            conn,
            dfPlcExcel
        )

        return jsonify({
            "success": True,
            "message": "Recipe Tag information successfully updated."
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"Error reading Excel file: {str(e)}"
        }), 500


@app.route('/api/backup/custom', methods=['GET'])
def create_custom_backup_route():
    try:
        from_date = request.args.get("from_date") or None
        to_date = request.args.get("to_date") or None

        # If only one of the two is provided, that's ambiguous — reject.
        if (from_date and not to_date) or (to_date and not from_date):
            return jsonify({
                "success": False,
                "message": "Please provide both From Date and To Date, or leave both empty for a full backup."
            }), 400

        if from_date and to_date:
            # Filtered backup
            custom_path, custom_name = db_management.create_custom_range_backup(
                from_date,
                to_date
            )
        else:
            # No filter provided -> full backup
            custom_path, custom_name = db_management.create_full_backup()

        return send_file(
            custom_path,
            as_attachment=True,
            download_name=custom_name,
            mimetype="application/octet-stream"
        )

    except ValueError as e:
        return jsonify({
            "success": False,
            "message": str(e)
        }), 400

    except FileNotFoundError:
        return jsonify({
            "success": False,
            "message": "Database file not found."
        }), 404

    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e)
        }), 500


@app.route('/export-model-excel', methods=['GET'])
def model_excel():
    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

    try:
        query = 'SELECT * FROM "Data"'
        df = pd.read_sql_query(query, conn)

        # Temporary file
        temp_file = tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".xlsx"
        )
        file_path = temp_file.name
        temp_file.close()

        with pd.ExcelWriter(file_path, engine='xlsxwriter') as writer:
            df.to_excel(
                writer,
                sheet_name='Sheet1',
                index=False
            )

        return send_file(
            file_path,
            as_attachment=True,
            download_name="Data.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

    finally:
        conn.close()


@app.route('/upload-plc-db', methods=['POST'])
def upload_plc_db():
    conn = None

    try:
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        

        if 'file' not in request.files:
            return jsonify({
                "success": False,
                "message": "No file uploaded"
            }), 400

        file = request.files['file']

        if not file or file.filename == '':
            return jsonify({
                "success": False,
                "message": "No file selected"
            }), 400

        # Read Excel; it must match the driver chosen in Settings
        dfPlcExcel = pd.read_excel(file)
        error = tag_table_error(dfPlcExcel, PLC_TAG_COLUMNS, "PLC tag")
        if error:
            return jsonify({"success": False, "message": error}), 400

        # Insert into Postgres
        postgres.insert_data_into_sqlite(
            cursorWrite,
            conn,
            dfPlcExcel
        )

        print("Data inserted into database table successfully.")

        # Reload PLC data
        softwaretype = request.form.get("softwaretype", "")

        dfPlcdb = postgres.dfPlc(
            conn,
            softwaretype
        )

        print(dfPlcdb)

        return jsonify({
            "success": True,
            "message": "PLC Data information successfully updated.",
            "rows": len(dfPlcExcel)
        })

    except Exception as e:
        if conn:
            conn.rollback()

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

    finally:
        if conn:
            conn.close()


def is_admin():
    return session.get("role") in ["admin", "superadmin"]


@app.route('/stocks')
def stocks():
    user_logged_in = 'username' in session
    username = session.get("username")
    user_role = session.get("role")

    return render_template(
        'stocks.html',
        user_logged_in=user_logged_in,
        user=username,
        role=user_role
    )


#  API Route — returns live data for the Stocks table
@app.route("/api/stocks", methods=["GET"])
def get_stocks_data():
    try:
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

        query = """
            SELECT "SiloNo", "MaterialName", "MaterialCode", "OperatorName", "TotalExtracted"
            FROM "MaterialData"
        """
        df = pd.read_sql(query, conn)

        # REMOVE rows where SiloNo is NULL, empty string, or '-'
        df = df[df["SiloNo"].notna()]
        df = df[df["SiloNo"].astype(str).str.strip() != ""]
        df = df[df["SiloNo"].astype(str).str.strip() != "-"]

        # Convert SiloNo to integer safely
        df["SiloNo"] = pd.to_numeric(df["SiloNo"], errors="coerce")
        df = df.dropna(subset=["SiloNo"])
        df["SiloNo"] = df["SiloNo"].astype(int)

        # Fill remaining NaN values
        df = df.fillna("")

        # Add unique id
        df.reset_index(drop=True, inplace=True)
        df.insert(0, "Id", df.index + 1)

        df_sorted = df.sort_values(by="SiloNo", ascending=True)

        return jsonify({
            "success": True,
            "records": df_sorted.to_dict(orient="records")
        })

    except Exception as e:
        print(" Error reading stock data:", e)
        return jsonify({"success": False, "error": str(e)})

    finally:
        try:
            conn.close()
        except:
            pass


# Add new stock
@app.route("/api/stocks/add", methods=["POST"])
def add_stock():
    try:
        data = request.get_json()
        silono = data["SiloNo"]

        operator_name = session.get("username", "Unknown")

        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

        cursorRead.execute('SELECT 1 FROM "MaterialData" WHERE "SiloNo" = %s', (silono,))
        if cursorRead.fetchone():
            return jsonify({"success": False, "error": f"SiloNo {silono} already exists."})

        cursorWrite.execute("""
            INSERT INTO "MaterialData" ("SiloNo", "MaterialName", "MaterialCode", "OperatorName")
            VALUES (%s, %s, %s, %s)
        """, (silono, data["MaterialName"], data["MaterialCode"], operator_name))

        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# Update existing stock
@app.route("/api/stocks/update/<string:old_silono>", methods=["PUT"])
def update_stock(old_silono):
    try:
        data = request.get_json()
        new_silono = data["SiloNo"]

        operator_name = session.get("username", "Unknown")

        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

        if old_silono != new_silono:
            cursorRead.execute('SELECT 1 FROM "MaterialData" WHERE "SiloNo" = %s', (new_silono,))
            if cursorRead.fetchone():
                return jsonify({"success": False, "error": f"SiloNo {new_silono} already exists."})

        cursorRead.execute('SELECT "MaterialName", "MaterialCode" FROM "MaterialData" WHERE "SiloNo" = %s',
                           (old_silono,))
        current = cursorRead.fetchone()
        if not current:
            return jsonify({"success": False, "error": f"SiloNo {old_silono} not found."})

        # A different material in the silo: its consumption starts again from 0
        clean = lambda v: str(v or "").strip()
        material_changed = (clean(current[0]) != clean(data["MaterialName"]) or
                            clean(current[1]) != clean(data["MaterialCode"]))

        cursorWrite.execute("""
            UPDATE "MaterialData"
            SET "SiloNo" = %s, "MaterialName" = %s, "MaterialCode" = %s, "OperatorName" = %s,
                "TotalExtracted" = CASE WHEN %s THEN '0' ELSE "TotalExtracted" END
            WHERE "SiloNo" = %s
        """, (new_silono, data["MaterialName"], data["MaterialCode"], operator_name,
              material_changed, old_silono))

        conn.commit()
        return jsonify({"success": True, "consumption_reset": material_changed})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# Delete stock by SiloNo
@app.route("/api/stocks/delete/<string:silono>", methods=["DELETE"])
def delete_stock(silono):
    try:
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        cursorWrite.execute('DELETE FROM "MaterialData" WHERE "SiloNo" = %s', (silono,))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        print("Delete Error:", e)
        return jsonify({"success": False, "error": str(e)})

    finally:
        conn.close()


@app.route('/api/stocks/export', methods=['POST'])
def export_material_data():
    try:
        print(" MaterialData Excel Export Requested")

        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

        query = """
            SELECT "SiloNo", "MaterialName", "MaterialCode", "OperatorName", "TotalExtracted"
            FROM "MaterialData"
        """
        df = pd.read_sql_query(query, conn)

        if df is None or df.empty:
            return jsonify({"success": False, "error": "No data available to export"}), 400

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name='MaterialData')

        output.seek(0)

        filename = f"MaterialData_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    except Exception as e:
        print(" Export Error:", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/about')
def about():
    from config.version import SOFTWARE_VERSION
    version = SOFTWARE_VERSION
    try:
        conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()
        cursorRead.execute('SELECT "Info" FROM "Info_db" WHERE "Particulars" = %s', ("Software_version",))
        row = cursorRead.fetchone()
        conn.close()
        if row and row[0]:
            version = row[0]
    except Exception as e:
        print(" About: could not read Software_version:", e)
    return render_template('about.html', app_version=version)


# --------------------------------- LICENCE ----------------------------------
# Paths that work without a licence: the activation screen itself, static
# files, and the status probe used by the Docker health check.
LICENCE_OPEN_PATHS = ('/static/', '/activation', '/activate_license', '/api/licence', '/plc_status')
LICENCE_ADMIN_ROLES = ('admin', 'superadmin')


@app.before_request
def licence_gate():
    if request.path.startswith(LICENCE_OPEN_PATHS):
        return None
    if licence.status()["valid"]:
        return None
    # Browser page loads go to the activation screen; fetch()/API calls get JSON.
    # (Browsers list text/html in Accept; fetch() sends */*. Don't use
    # accept_mimetypes.best - for Chrome/Edge it is application/signed-exchange.)
    is_page_load = request.method == 'GET' and 'text/html' in request.headers.get('Accept', '')
    if not is_page_load:
        return jsonify(success=False, licence=False, message=licence.status()["message"]), 403
    return redirect(url_for('activation'))


@app.route('/activation')
def activation():
    status = licence.status(force=True)
    # First activation (or an expired demo): anyone at the machine may enter a key.
    # Replacing a working key (e.g. demo -> purchased) needs an admin login.
    can_change = not status["valid"] or session.get('role') in LICENCE_ADMIN_ROLES
    return render_template('activation.html', licence=status, can_change=can_change)


@app.route('/activate_license', methods=['POST'])
def activate_license():
    if licence.status()["valid"] and session.get('role') not in LICENCE_ADMIN_ROLES:
        return jsonify(success=False, message="Only an admin can change the licence key"), 403

    key = (request.get_json(silent=True) or {}).get('licenseKey', '')
    success, message = licence.activate(key)
    if success and not monitor.is_running():
        monitor.start_auto_connect()     # start PLC logging now that we are licensed
    return jsonify(success=success, message=message), (200 if success else 400)


@app.route('/api/licence')
def licence_info():
    status = licence.status()
    return jsonify(valid=status["valid"], type=status["type_name"], message=status["message"],
                   days_left=status["days_left"], machine_id=status["machine_id"],
                   expires=status["expires"].isoformat() if status["expires"] else None)


@app.route('/super_admin')
def super_admin():
    user_logged_in = 'username' in session
    df = sqliteCon.dfUser()
    df['Is_Active'] = df.apply(
        lambda row: (
            f"<input type='checkbox' class='toggle-active' data-userid='{row['Id']}' {'checked' if row['Is_Active'] == 1 else ''} />"
        ),
        axis=1
    )

    # Hide real DB Id
    df = df.drop(columns=['Id'])

    table_html = df.to_html(classes='table table-striped', index=False, escape=False, table_id='inventory-table')
    return render_template('super_admin.html', table=table_html, user_logged_in=user_logged_in)


# --------------------------------- LOGIN ------------------------------------

@app.route('/login', methods=['POST'])
def login():
    username = request.form["username"]
    password = request.form["password"]

    user = authLog.get_user(username)
    # user = (id, username, password_hash, role, user_access, is_active, last_login)
    print(user)

    if user and check_password_hash(user[2], password):
        if user[5] == 1:  # active?
            session.permanent = True
            session['username'] = user[1]
            session['role'] = user[3]
            return jsonify(success=True)

        return jsonify(success=False, error="Your account is deactivated."), 403

    return jsonify(success=False, error="Invalid Credentials"), 403


# --------------------- USER SELF CHANGE PASSWORD ----------------------------

@app.route('/change_password', methods=['POST'])
def change_password():
    if 'username' not in session:
        return jsonify(success=False, error="Not logged in"), 403

    data = request.get_json()
    old_password = data.get("oldPassword")
    new_password = data.get("newPassword")

    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

    cursorRead.execute("SELECT password_hash FROM users WHERE username=%s", (session['username'],))
    row = cursorRead.fetchone()
    if not row:
        return jsonify(success=False, error="User not found")

    if not check_password_hash(row[0], old_password):
        return jsonify(success=False, error="Old password incorrect")

    new_hash = generate_password_hash(new_password)
    cursorWrite.execute("UPDATE users SET password_hash=%s WHERE username=%s", (new_hash, session['username']))
    conn.commit()
    conn.close()

    return jsonify(success=True)


# ---------------------- ADMIN UPDATE USER PASSWORD --------------------------

@app.route('/update_user_password', methods=['POST'])
def update_user_password():
    data = request.get_json()
    username = data.get("username")
    new_password = data.get("new_password")

    if not username or not new_password:
        return jsonify(success=False, error="Invalid data")

    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

    hashed = generate_password_hash(new_password)
    cursorWrite.execute("UPDATE users SET password_hash=%s WHERE username=%s", (hashed, username))

    conn.commit()
    conn.close()
    return jsonify(success=True)


# ---------------------- ADMIN UPDATE USER ACCESS ----------------------------

@app.route("/update_user_details", methods=["POST"])
def update_user_details():
    data = request.get_json()
    username = data.get("username")
    user_access = data.get("user_access")

    if not username or not user_access:
        return jsonify(success=False, error="Missing fields")

    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

    cursorWrite.execute("UPDATE users SET user_access=%s WHERE username=%s", (user_access, username))
    conn.commit()
    conn.close()

    return jsonify(success=True)


# ------------------------------ ADD USER -----------------------------------

@app.route("/add_user", methods=["POST"])
def add_user():
    data = request.get_json()
    username = data.get("username")
    role = data.get("role")
    print("Adding user:", username, "with role:", role)

    if not username or not role:
        return jsonify(success=False, error="Missing fields")

    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

    # default password
    hashed = generate_password_hash("12345678")

    try:
        cursorWrite.execute("""
            INSERT INTO users (username, password_hash, role, is_active)
            VALUES (%s, %s, %s, 1)
        """, (username, hashed, role))

        conn.commit()
        conn.close()
        return jsonify(success=True)

    except psycopg2.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify(success=False, error="User already exists")


# ----------------------------- TOGGLE ACTIVE -------------------------------

@app.route("/toggle_user_active", methods=["POST"])
def toggle_user_active():
    data = request.get_json()
    user_id_raw = data.get("user_id")
    is_active = data.get("is_active")

    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

    try:
        user_id = int(user_id_raw)
    except (TypeError, ValueError):
        conn.close()
        return jsonify(success=False, error=f"Invalid user_id: {user_id_raw!r}")

    try:
        cursorWrite.execute("UPDATE users SET is_active=%s WHERE id=%s", (is_active, user_id))
        conn.commit()
    except psycopg2.Error as e:
        conn.rollback()   # important — failed query leaves the transaction aborted
        conn.close()
        return jsonify(success=False, error="Database error updating user")
    finally:
        if not conn.closed:
            conn.close()

    return jsonify(success=True)


# ----------------------------- DELETE USER ---------------------------------

@app.route("/delete_user", methods=["POST"])
def delete_user():
    data = request.get_json()
    username = data.get("username")

    if not username:
        return jsonify(success=False, error="Missing username")

    conn, cursorRead, cursorWrite = sqliteCon.get_db_connection()

    cursorWrite.execute("DELETE FROM users WHERE username=%s", (username,))
    conn.commit()
    conn.close()

    return jsonify(success=True)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))


# ==========================================
# PLC Connect / Disconnect / Status
# ==========================================
@app.route('/start_plc', methods=['POST'])
def start_plc():
    data = request.get_json(silent=True) or {}
    try:
        driver = int(data.get('driver'))
    except (TypeError, ValueError):
        return jsonify(success=False, status="disconnected", message="Invalid driver"), 200

    station_ip = (data.get('station_ip') or "").strip() or None
    success, message = monitor.start_monitoring(driver, station_ip)

    return jsonify(success=success,
                   status="connected" if success else "disconnected",
                   message=message), 200


@app.route('/stop_plc', methods=['POST'])
def stop_plc():
    if not monitor.is_running():
        return jsonify(success=False, status="disconnected",
                       message=monitor.MSG_NOT_CONNECTED), 200

    monitor.stop_monitoring()
    return jsonify(success=True, status="disconnected",
                   message="PLC Disconnected Successfully"), 200


@app.route('/plc_status')
def plc_status():
    # connected | reconnecting (PLC link dropped, monitor retrying) | disconnected
    return jsonify(status=monitor.get_status())


@app.route('/api/settings/get_plc_config')
def get_plc_config():
    try:
        node = monitor.get_saved_node()
        return jsonify(station_ip=node, driver=monitor.get_saved_driver(node))
    except Exception:
        return jsonify(station_ip="", driver=1)


@app.route('/upload_excel', methods=['POST'])
def openXl():
    try:
        if 'file' not in request.files:
            return jsonify({
                "status": "error",
                "message": "No file uploaded."
            }), 400

        file = request.files['file']

        if file.filename == '':
            return jsonify({
                "status": "error",
                "message": "No file selected."
            }), 400

        # Read Excel directly from uploaded file
        dfPlcExcel = pd.read_excel(file)

        print(dfPlcExcel)

        cursorRead, cursorWrite, engineConRead, engineConWriten, conn = postgres.sqlite()

        postgres.insert_data_into_sqlite_rec(
            cursorWrite,
            conn,
            dfPlcExcel
        )

        print("Data inserted into database table successfully.")

        return jsonify({
            "status": "success",
            "message": "PLC Tag information successfully updated."
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"Error reading Excel file: {str(e)}"
        }), 500


@app.route('/api/recipe_download/preview', methods=['POST'])
def recipe_download_preview():
    """What will be written, for the confirmation popup. Does not touch the PLC."""
    data = request.get_json(silent=True) or {}
    result = recipewrite.preview_download(data.get("mixerno"), data.get("recipe_name"),
                                          monitor.get_saved_driver())
    return jsonify(result), (200 if result["success"] else 400)


@app.route('/download_recipe', methods=['POST'])
def download_recipe():
    try:
        data = request.get_json(silent=True) or {}
        # Driver = the one chosen in Settings (saved in Info_db), not the browser session
        result = recipewrite.writePlcRecipe(data.get("mixerno"), data.get("recipe_name"),
                                            monitor.get_saved_driver())
        return jsonify(result), (200 if result.get("success") else 400)

    except Exception as e:
        logging.exception("download_recipe failed")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/settings/set_driver', methods=['POST'])
def set_driver():
    if session.get('role') not in ('admin', 'superadmin'):
        return jsonify(success=False, message="Only an admin can change the PLC driver"), 403
    try:
        driver = int((request.get_json(silent=True) or {}).get('driver'))
    except (TypeError, ValueError):
        driver = None
    if driver not in monitor.DRIVER_NAMES:
        return jsonify(success=False, message="Driver must be 1 (Siemens) or 2 (Rockwell)"), 400
    monitor.save_plc_config(driver=driver)
    return jsonify(success=True, driver=driver, driver_name=monitor.DRIVER_NAMES[driver])


@app.errorhandler(403)
def forbidden(e):
    return render_template('403.html'), 403


@app.before_request
def load_user():
    g.user = session.get('username')
    g.role = session.get('role')


@app.context_processor
def inject_user():
    return dict(user=session.get('username'), role=session.get('role'), licence=licence.status())


if __name__ == "__main__":
    # Try once at startup. If the PLC is off or unreachable it just stays
    # "Disconnected" (see plc_monitor.log) and the user can press Connect later.
    try:
        postgres.ensure_indexes()
    except Exception:
        logging.exception("Could not create database indexes")
    monitor.start_auto_connect()

    app.run(debug=True, use_reloader=False)