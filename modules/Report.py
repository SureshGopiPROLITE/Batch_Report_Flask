import os

os.environ["GIO_USE_VFS"] = "local"
os.environ["GDK_BACKEND"] = "none"
os.environ["NO_AT_BRIDGE"] = "1"
os.environ["WEASYPRINT_GUI"] = "false"

import html as html_lib
import pandas as pd
import base64
from datetime import datetime
import pdfkit
from weasyprint import HTML, CSS

import io
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
import platform


# ==========================================================
# 🔹 Utility Functions
# ==========================================================

def check(set_wg, act_wg, tol):
    if set_wg - tol <= act_wg <= set_wg + tol:
        return "Good"

    return "Up" if act_wg > set_wg + tol else "Down"


def difference(set_wg, act_wg):
    return round(act_wg - set_wg, 2)


def _blank(value):
    """Empty cell instead of 'nan'/'None' (e.g. batches logged before a tag existed)."""
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else value


_logo_cache = {}
LOGO_MAX_HEIGHT = 300   # px - plenty for the report header, at print resolution


def encode_logo(logo_path):
    """Logo as Base64 PNG for embedding, scaled down to print size.

    The uploaded logo can be huge (12500 x 3125 px, 2.6 MB); decoding that for
    every report page made PDFs slow and large. Cached until the file changes."""
    try:
        stamp = os.path.getmtime(logo_path)
    except OSError:
        stamp = None
    cached = _logo_cache.get(logo_path)
    if cached and cached[0] == stamp:
        return cached[1]

    with open(logo_path, "rb") as logo_file:
        raw = logo_file.read()
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        if img.height > LOGO_MAX_HEIGHT:
            img = img.resize((max(1, round(img.width * LOGO_MAX_HEIGHT / img.height)), LOGO_MAX_HEIGHT),
                             Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            raw = buf.getvalue()
    except Exception:
        pass   # unreadable by Pillow - embed the original file
    encoded = base64.b64encode(raw).decode("utf-8")
    _logo_cache[logo_path] = (stamp, encoded)
    return encoded


# ==========================================================
# 🔹 PDF Report Generator
# ==========================================================

# A4 portrait, content fills the printable width (the old scale(0.86) left
# it small and off-centre); page number at the bottom of every page
PDF_PAGE_CSS = """
    @page {
        size: A4;
        margin: 12mm 10mm 14mm 10mm;
        @bottom-right {
            content: "Page " counter(page) " of " counter(pages);
            font-family: "DejaVu Sans", Arial, sans-serif;
            font-size: 7.5pt;
            color: #6b7280;
        }
    }
"""


def _report_details(df_string, df_cal_sum, batch_no):
    """Header values of one batch (Info + Summary rows)."""
    def get_value(k):
        hit = df_string.loc[df_string["Name"] == k, "Value"] if not df_string.empty else []
        return hit.iloc[0] if len(hit) else "N/A"

    def get_cal_value(k):
        hit = df_cal_sum.loc[df_cal_sum["Name"] == k, "Value"] if not df_cal_sum.empty else []
        return hit.iloc[0] if len(hit) else "N/A"

    return {
        "printed_date": datetime.now().strftime("%d-%m-%Y %H:%M"),
        "plant_name": get_value("Plant Name"),
        "recipe_name": get_value("Recipe Name"),
        "start_time": get_value("Start Date Time"),
        "end_time": get_value("End Date Time"),
        "shift": get_value("Shift"),
        "mixer_no": get_value("Mixer Selected"),
        "batch_no": batch_no,
        "time_taken": get_cal_value("BatchTimeMinutes"),
        "total_set_weight": get_cal_value("TotalBatchSetWeight"),
        "total_actual_weight": get_cal_value("TotalBatchActualWeight"),
    }


def _render_pdf(df_pivot, df_string, batch_no, df_cal_sum, include_speed=True, logo_base64=None):
    """One batch report, rendered (WeasyPrint document - pages can be merged)."""
    if logo_base64 is None:
        logo_base64 = encode_logo("data_files/logo.png")
    html = generate_html_report(
        df_pivot,
        logo_base64,
        _report_details(df_string, df_cal_sum, batch_no),
        include_speed=include_speed
    )
    return HTML(string=html).render(stylesheets=[CSS(string=PDF_PAGE_CSS)])


# ==========================================================
# 🔹 PDF Report Generator
# ==========================================================

def generate_pdf_report(
    df_pivot,
    df_string,
    batch_no,
    df_cal_sum,
    include_speed=True
):
    return _render_pdf(df_pivot, df_string, batch_no, df_cal_sum, include_speed).write_pdf()


def generate_multi_pdf_report(batches, include_speed=True):
    """batches: [(batch_no, df_pivot, df_string, df_cal_sum), ...] ->
    one PDF, every batch report starting on its own page."""
    logo_base64 = encode_logo("data_files/logo.png")
    docs = [_render_pdf(pivot, string, no, cal, include_speed, logo_base64)
            for no, pivot, string, cal in batches]
    pages = [page for doc in docs for page in doc.pages]
    return docs[0].copy(pages).write_pdf()


# ==========================================================
# 🔹 Excel Report Generator
# ==========================================================

def _fill_excel_sheet(ws, df_pivot, details, include_speed=True):
    """Writes one batch report onto worksheet ws."""
    # ======================================================
    # 🔹 Report Title
    # ======================================================
    ws.append(["BATCH REPORT"])
    ws.merge_cells("A1:F1")
    ws["A1"].font = Font(
        size=14,
        bold=True
    )
    ws["A1"].alignment = Alignment(
     horizontal="center"
    )

    # ======================================================
    # 🔹 Report Information
    # ======================================================
    ws.append([
        "Printed Date:",
        details["printed_date"]
    ])
    ws.append([])
    headers = [
        ("Plant Name",details["plant_name"]),
        ("Recipe Name",details["recipe_name"]),
        ("Batch No",details["batch_no"]),
        ("Mixer No",details["mixer_no"]),
        ("Start Time",details["start_time"]),
        ("End Time",details["end_time"]),
        ("Shift",details["shift"]),
        ("Total Set Weight (Kg)",
        df_pivot["SetWeight"].sum()),
        ("Total Actual Weight (Kg)",round(df_pivot["ActualWeight"].sum(),2))
    ]
    for i in range(
        0,
        len(headers),
        2
    ):
        row = [
            headers[i][0],
            headers[i][1]
        ]
        if i + 1 < len(headers):
            row += [
                headers[i + 1][0],
                headers[i + 1][1]
            ]
        ws.append(row)
    ws.append([])
    # ======================================================
    # 🔹 Table Headers
    # ======================================================

    table_headers = [
        "Silo No",
        "Material Name",
        "Set Weight",
        "Actual Weight",
        "Difference",
        "Tolerance",
        "Inflight Weight"
    ]

    # Add speed columns only when enabled
    if include_speed:
        table_headers += [
            "CoarseSpeed",
            "FineSpeed"
        ]
    table_headers += [
        "Start Time",
        "End Time",
        "Duration (hh:mm:ss)"
    ]
    ws.append(table_headers)

    # ======================================================
    # 🔹 Header Formatting
    # ======================================================

    header_fill = PatternFill(
        start_color="DDDDDD",
        fill_type="solid"
    )
    for col in range(1,len(table_headers) + 1):
        cell = ws.cell(
            row=ws.max_row,
            column=col
        )
        cell.font = Font(
            bold=True
        )
        cell.alignment = Alignment(
            horizontal="center"
        )
        cell.fill = header_fill

    # ======================================================
    # 🔹 Add Data Rows
    # ======================================================
    for _, row in df_pivot.iterrows():

        row_data = [
        row.get("SiloNo",""),
        row.get("MaterialName",""),
        row.get("SetWeight",""),
        row.get("ActualWeight",""),
        row.get("Difference",""),
        row.get("Tolerance",""),
        _blank(row.get("InflightWeight"))
        ]

        # Add speed values only when enabled
        if include_speed:
            row_data += [
                row.get(
                    "CoarseSpeed",
                    ""
                ),
                row.get(
                    "FineSpeed",
                    ""
                )
            ]
        row_data += [
            row.get("StartTime", ""),
            row.get("EndTime", ""),
            row.get("Duration", "")
        ]
        ws.append(row_data)



def generate_excel_report(
    df_pivot,
    df_string,
    batch_no,
    df_cal_sum,
    include_speed=True
):
    output = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "Batch Report"
    _fill_excel_sheet(ws, df_pivot, _report_details(df_string, df_cal_sum, batch_no), include_speed)
    wb.save(output)
    output.seek(0)
    return output.read()


def generate_multi_excel_report(batches, summary=None, include_speed=True):
    """One workbook: a "Summary" sheet (one row per batch) and one sheet per
    batch with the same layout as the single batch report."""
    output = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    if summary is not None and not summary.empty:
        ws.append(list(summary.columns))
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill(start_color="DDDDDD", fill_type="solid")
        for row in summary.itertuples(index=False):
            ws.append(list(row))
        for i, col in enumerate(summary.columns, start=1):
            width = max([len(str(col))] + [len(str(v)) for v in summary.iloc[:, i - 1].head(300)]) + 2
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(width, 40)
        ws.freeze_panes = "A2"

    for no, pivot, string, cal in batches:
        sheet = wb.create_sheet(title=f"Batch {no}"[:31])
        _fill_excel_sheet(sheet, pivot, _report_details(string, cal, no), include_speed)

    wb.save(output)
    output.seek(0)
    return output.read()

# ==========================================================
# 🔹 HTML Template for PDF
# ==========================================================

# Fields of the batch header, in the order of the PDF / Excel header block:
# left column, then right column
HEADER_FIELDS = [
    ("Recipe Name", "recipe_name"),
    ("Batch No", "batch_no"),
    ("Start Time", "start_time"),
    ("End Time", "end_time"),
    ("Mixer No", "mixer_no"),
    ("Time Taken(Min)", "time_taken"),
    ("Total Set Weight(Kg)", "total_set_weight"),
    ("Total Actual Weight(Kg)", "total_actual_weight"),
    ("Shift", "shift"),
    ("Plant Name", "plant_name"),
]


def _h(value):
    """Text for the PDF HTML: escaped, empty for nan/None."""
    return html_lib.escape(str(_blank(value)))


# Shared by the batch report PDF and the header-only PDF. No web fonts:
# plant PCs are often offline (the font request would only time out).
REPORT_CSS = """<style>
    /* No web fonts: plant PCs are often offline (the request would only time out) */
    body {
        margin: 0;
        font-family: "DejaVu Sans", "Segoe UI", Arial, sans-serif;
        font-size: 9.5pt;
        color: #1f2937;
    }

    /* ---- title bar ---- */
    .title-bar { width: 100%; border-collapse: collapse; margin-bottom: 4mm; }
    .title-bar td { border: none; padding: 0; vertical-align: middle; }
    .title-bar .logo img { height: 11mm; }
    .title-bar .title { text-align: center; font-size: 18pt; font-weight: bold;
                         letter-spacing: 1px; color: #111827; }
    .title-bar .printed { text-align: right; font-size: 8.5pt; color: #4b5563; white-space: nowrap; }
    .rule { border: 0; border-top: 2px solid #005da2; margin: 0 0 4mm; }

    /* ---- batch header ---- */
    .info { width: 100%; border-collapse: collapse; margin-bottom: 5mm;
             border: 1px solid #d6dde6; background: #f7f9fc; }
    .info th, .info td { border: none; padding: 1.6mm 2.5mm; text-align: left; font-size: 9.5pt; }
    .info th { width: 21%; color: #374151; font-weight: bold; white-space: nowrap; }
    .info td { width: 29%; }

    h2 { font-size: 11pt; margin: 0 0 2mm; color: #005da2; }

    /* ---- data table ---- */
    .data { width: 100%; border-collapse: collapse; }
    .data thead { display: table-header-group; }     /* repeat on every page */
    .data tr { page-break-inside: avoid; }
    .data th { background: #005da2; color: #fff; font-size: 8pt; font-weight: bold;
                padding: 1.8mm 1.2mm; border: 1px solid #005da2; text-align: center; }
    .data td { border: 1px solid #d6dde6; padding: 1.5mm 1.2mm; font-size: 8.5pt;
                text-align: center; white-space: nowrap; }
    .data td.left { text-align: left; white-space: normal; }
    .data tbody tr:nth-child(even) td { background: #f5f8fb; }

    .footer { text-align: center; font-size: 7.5pt; color: #6b7280; margin-top: 4mm; }
</style>
"""


def _title_bar(logo_base64, title, printed_date):
    return f"""
    <table class="title-bar">
        <tr>
            <td class="logo" style="width:30%"><img src="data:image/png;base64,{logo_base64}" alt="Logo"></td>
            <td class="title" style="width:40%">{_h(title)}</td>
            <td class="printed" style="width:30%">Printed: {_h(printed_date)}</td>
        </tr>
    </table>
    <hr class="rule">"""


def _header_block(details):
    """The batch header: 5 rows of label | value | label | value."""
    def value(key):
        v = _h(details.get(key))
        if key in ("total_set_weight", "total_actual_weight") and v not in ("", "N/A"):
            v += " Kg"
        return v

    half = len(HEADER_FIELDS) // 2
    rows = "".join(
        f"<tr><th>{l1}</th><td>{value(k1)}</td><th>{l2}</th><td>{value(k2)}</td></tr>"
        for (l1, k1), (l2, k2) in zip(HEADER_FIELDS[:half], HEADER_FIELDS[half:])
    )
    return f'<table class="info">{rows}</table>'


def generate_html_report(
    df,
    logo_base64,
    details,
    include_speed=True
):
    # ---------------- data rows ----------------
    data_rows = ""
    for _, row in df.iterrows():
        cells = [
            f'<td>{_h(row.get("SiloNo"))}</td>',
            f'<td class="left">{_h(row.get("MaterialName"))}</td>',
            f'<td>{_h(row.get("SetWeight"))}</td>',
            f'<td>{_h(row.get("ActualWeight"))}</td>',
            f'<td>{_h(row.get("Difference"))}</td>',
            f'<td>{_h(row.get("Tolerance"))}</td>',
            f'<td>{_h(row.get("InflightWeight"))}</td>',
        ]
        if include_speed:
            cells += [f'<td>{_h(row.get("CoarseSpeed"))}</td>',
                      f'<td>{_h(row.get("FineSpeed"))}</td>']
        cells += [f'<td>{_h(row.get("StartTime"))}</td>',
                  f'<td>{_h(row.get("EndTime"))}</td>',
                  f'<td>{_h(row.get("Duration"))}</td>']
        data_rows += "<tr>" + "".join(cells) + "</tr>\n"

    speed_headers = ("<th>Coarse Speed<br>(%)</th><th>Fine Speed<br>(%)</th>"
                     if include_speed else "")

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Batch Report</title>
{REPORT_CSS}
</head>
<body>
    {_title_bar(logo_base64, "BATCH REPORT", details['printed_date'])}

    {_header_block(details)}

    <h2>Data Table</h2>
    <table class="data">
        <thead>
            <tr>
                <th>Silo<br>No</th>
                <th>Material Name</th>
                <th>Set Weight<br>(Kg)</th>
                <th>Actual Weight<br>(Kg)</th>
                <th>Difference<br>(Kg)</th>
                <th>Tolerance<br>(Kg)</th>
                <th>Inflight Weight<br>(Kg)</th>
                {speed_headers}
                <th>Start<br>Time</th>
                <th>End<br>Time</th>
                <th>Duration<br>(hh:mm:ss)</th>
            </tr>
        </thead>
        <tbody>
            {data_rows}
        </tbody>
    </table>

    <div class="footer">
        This report is generated by Skew Reporting Software, developed by Prolite Automation.
    </div>
</body>
</html>
"""


# ==========================================================
# 🔹 Batch header PDF (Report "Batch Label")
# ==========================================================

def generate_header_pdf(batches):
    """batches: [(batch_no, df_string, df_cal_sum), ...] -> PDF bytes with
    only the batch header (the block on top of the batch report). Several
    batches: their blocks one after another, never split across a page."""
    logo_base64 = encode_logo("data_files/logo.png")
    details = [_report_details(string, cal, no) for no, string, cal in batches]
    title = "BATCH LABEL" if len(details) == 1 else f"BATCH LABELS ({len(details)})"
    blocks = "".join(f'<div class="header-item">{_header_block(d)}</div>' for d in details)
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Batch Label</title>
{REPORT_CSS}
<style>
    .header-item {{ page-break-inside: avoid; }}
    .header-item .info {{ margin-bottom: 4mm; }}
</style>
</head>
<body>
    {_title_bar(logo_base64, title, details[0]['printed_date'])}
    {blocks}
    <div class="footer">
        This report is generated by Skew Reporting Software, developed by Prolite Automation.
    </div>
</body>
</html>
"""
    return HTML(string=html).render(stylesheets=[CSS(string=PDF_PAGE_CSS)]).write_pdf()
