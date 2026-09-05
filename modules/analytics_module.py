import dash
from dash import dcc, html, Input, Output
import plotly.express as px
import pandas as pd
import numpy as np
from datetime import datetime
import sqlite3
from sqlalchemy import create_engine

DB_CONFIG = "PLCDB2.db"


# === Direct SQLite Connection (for raw cursor use) ===
def get_db_connection():
    try:
        conn = sqlite3.connect(DB_CONFIG)
        conn.row_factory = sqlite3.Row  # Dict-like cursor
        cursorRead = conn.cursor()
        cursorWrite = conn.cursor()
        return conn, cursorRead, cursorWrite
    except Exception as e:
        print(f"Failed to connect to SQLite: {e}")
        return None



# === SQLAlchemy Engine for pandas.to_sql and read_sql ===
def get_db_connection_engine():
    try:
        # SQLite connection URL
        db_url = f"sqlite:///{DB_CONFIG}"

        # Create SQLAlchemy engine
        engine = create_engine(db_url, echo=False)

        # Separate read and write connections
        engineConRead = engine.connect()
        engineConWrite = engine.connect()

        print("✅ SQLite SQLAlchemy engine created successfully.")
        return engine, engineConRead, engineConWrite

    except Exception as e:
        print(f"❌ Failed to create SQLAlchemy engine: {e}")
        return None, None, None

# -------------------- DATA CLEANING --------------------
def get_cleaned_data():
    # ✅ Get DB connections
    conn, cursorRead, cursorWrite = get_db_connection()
    engine, engineConRead, engineConWrite = get_db_connection_engine()

    df = pd.read_sql("SELECT * FROM plc_data", engineConRead)
    print("🔹 Raw data loaded:", df.shape)

    # Ensure copy to avoid chaining issues
    df1 = df[~((df['Category'] == "Info") | (df['DataType'] == "STRING"))].copy()

    df1['Value_num'] = pd.to_numeric(df1['Value'], errors='coerce')
    df1 = df1.drop('Value', axis=1)

    df1['TimeStamp_Format'] = pd.to_datetime(df1['TimeStamp'])
    df1 = df1.drop(['TimeStamp', 'DataType'], axis=1)

    df_pivot = df1.pivot_table(
        index=['BatchNo', 'Category'],
        columns=['Name'],
        values='Value_num'
    ).copy()

    df_pivot['Error_Kg'] = df_pivot['ActualWeight'] - df_pivot['SetWeight']
    df_pivot['Error_%'] = (df_pivot['Error_Kg'] / df_pivot['SetWeight']).abs() * 100

    # Outlier removal
    Q1, Q3 = df_pivot['Error_%'].quantile([0.25, 0.75])
    IQR = Q3 - Q1
    df_clean = df_pivot[
        (df_pivot['Error_%'] >= Q1 - 1.5 * IQR) &
        (df_pivot['Error_%'] <= Q3 + 1.5 * IQR)
    ].copy()

    df_reset = df_clean.reset_index()

    # Batch error
    batch_error = (
        df_reset.groupby('BatchNo')['Error_%']
        .mean()
        .reset_index()
        .rename(columns={'Error_%': 'Avg_Error_%'})
    )

    # Recipe info
    df_recipe = df.pivot_table(
        index=['BatchNo', 'Category'],
        columns='Name',
        values='Value',
        aggfunc='first'
    ).reset_index()

    df_recipe = (
        df_recipe[['BatchNo', 'Recipe Name', 'Start Date Time', 'End Date Time']]
        .dropna()
        .drop_duplicates()
        .copy()
    )

    merged = pd.merge(df_recipe, batch_error, on='BatchNo', how='inner')
    merged['Avg_Error_%'] = np.round(merged['Avg_Error_%'], 2)

    # Outliers again
    Q1, Q3 = merged['Avg_Error_%'].quantile([0.25, 0.75])
    IQR = Q3 - Q1

    df_cleaned = merged[
        (merged['Avg_Error_%'] >= Q1 - 1.5 * IQR) &
        (merged['Avg_Error_%'] <= Q3 + 1.5 * IQR)
    ].copy()

    # ❌ OLD: inplace=True ⇒ warning
    # df_cleaned.drop_duplicates(subset=['End Date Time'], inplace=True)

    # ✅ FIXED
    df_cleaned = df_cleaned.drop_duplicates(subset=['End Date Time']).copy()

    # ❌ OLD: warning
    # df_cleaned['Rank'] = np.random.randint(1, 5, len(df_cleaned))

    # ✅ FIXED using .loc
    if 'Rank' not in df_cleaned.columns:
        df_cleaned.loc[:, 'Rank'] = np.random.randint(1, 5, len(df_cleaned))

    print(f"✅ Cleaned dataset ready: {df_cleaned.shape}")
    return df_cleaned

# -------------------- DASH APP --------------------
def run_dashboard():
    df_cleaned = get_cleaned_data()

    df_cleaned = df_cleaned.copy()
    df_cleaned['Recipe Name'] = df_cleaned['Recipe Name'].astype(str).fillna('Nil')
    df_cleaned.loc[df_cleaned['Recipe Name'].str.strip() == '', 'Recipe Name'] = 'Nil'
    df_cleaned['Recipe Name'] = df_cleaned['Recipe Name'].apply(lambda x: str(x).strip())

    df_cleaned['Start Date Time'] = pd.to_datetime(df_cleaned['Start Date Time'])
    df_cleaned['End Date Time'] = pd.to_datetime(df_cleaned['End Date Time'])
    df_cleaned['Rank'] = df_cleaned['Rank'].astype(str)

    min_date = df_cleaned['End Date Time'].dt.date.min()
    max_date = df_cleaned['End Date Time'].dt.date.max()

    app = dash.Dash(__name__)
    app.title = "Recipe Error Analysis"

    # -------------------- LAYOUT --------------------
    app.layout = html.Div([
        html.Div([
            html.Div("📊", className="icon-box"),
            html.H1("Trend and Error Analysis of Recipes", className="main-title")
        ], className="header-title"),

        html.Div([
            html.Div([
                html.Label("Start Date"),
                dcc.DatePickerSingle(
                    id='start-date',
                    min_date_allowed=min_date,
                    max_date_allowed=max_date,
                    date=min_date,
                    display_format='YYYY-MM-DD'
                )
            ], className='filter-card'),

            html.Div([
                html.Label("End Date"),
                dcc.DatePickerSingle(
                    id='end-date',
                    min_date_allowed=min_date,
                    max_date_allowed=max_date,
                    date=max_date,
                    display_format='YYYY-MM-DD'
                )
            ], className='filter-card'),

            html.Div([
                html.Label("Recipe Name"),
                dcc.Dropdown(
                    id='recipe-filter',
                    options=[{'label': r, 'value': r} for r in sorted(df_cleaned['Recipe Name'].unique())],
                    multi=True,
                    placeholder="Select Recipe"
                )
            ], className='filter-card'),

            html.Div([
                html.Label("Rank"),
                dcc.Dropdown(
                    id='rank-filter',
                    options=[{'label': str(r), 'value': str(r)} for r in sorted(df_cleaned['Rank'].unique())],
                    multi=True,
                    placeholder="Select Rank"
                )
            ], className='filter-card'),
        ], className='filter-container'),

        html.Hr(style={'border': '1px solid #E5E7EB', 'marginTop': '20px'}),

        html.Div([
            html.Div(dcc.Graph(id='error-line-graph', style={'height': '500px'}), className='graph-card'),
            html.Div(dcc.Graph(id='error-bar-graph', style={'height': '500px'}), className='graph-card')
        ], className='graph-container')
    ])

    # -------------------- CALLBACK --------------------
    @app.callback(
        Output('error-line-graph', 'figure'),
        Output('error-bar-graph', 'figure'),
        Input('start-date', 'date'),
        Input('end-date', 'date'),
        Input('recipe-filter', 'value'),
        Input('rank-filter', 'value')
    )
    def update_graphs(start_date, end_date, selected_recipes, selected_ranks):
        df = df_cleaned.copy()

        if start_date:
            df = df[df['End Date Time'] >= pd.to_datetime(start_date)]
        if end_date:
            df = df[df['End Date Time'] <= pd.to_datetime(end_date) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)]

        if selected_recipes:
            df = df[df['Recipe Name'].isin(selected_recipes)]
        if selected_ranks:
            df = df[df['Rank'].isin([str(r) for r in selected_ranks])]

        # --- Line Chart ---
        if not df.empty:
            fig_line = px.line(
                df.sort_values('End Date Time'),
                x='End Date Time',
                y='Avg_Error_%',
                markers=True,
                hover_data=['Recipe Name', 'Rank'],
                title='Average Error % Over Time',
                color_discrete_sequence=['#2563EB']
            )
            fig_line.update_traces(line=dict(width=2))
            fig_line.update_layout(
                template='plotly_white',
                title_x=0.5,
                paper_bgcolor='white',
                plot_bgcolor='white',
                font=dict(color='#000000', size=14),
                xaxis=dict(showgrid=True, gridcolor='rgba(0,0,0,0.1)'),
                yaxis=dict(showgrid=True, gridcolor='rgba(0,0,0,0.1)')
            )
        else:
            fig_line = px.line(title="No data available")

        # --- Bar Chart ---
        if not df.empty:
            df_bar = df.groupby('Recipe Name', as_index=False)['Avg_Error_%'].mean()
            fig_bar = px.bar(
                df_bar,
                x='Recipe Name', y='Avg_Error_%',
                title='Average Error by Recipe',
                text_auto='.2f',
                color='Avg_Error_%',
                color_continuous_scale=px.colors.sequential.Viridis
            )
            fig_bar.update_layout(
                template='plotly_white',
                title_x=0.5,
                paper_bgcolor='white',
                plot_bgcolor='white',
                font=dict(color='#000000', size=13),
                xaxis=dict(tickangle=-45, showgrid=True, gridcolor='rgba(0,0,0,0.1)'),
                yaxis=dict(showgrid=True, gridcolor='rgba(0,0,0,0.1)')
            )
        else:
            fig_bar = px.bar(title="No data available")

        return fig_line, fig_bar

    # -------------------- CSS --------------------
    app.index_string = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Recipe Analysis Dashboard</title>
        <style>
            body { background-color: #EDF2F7; font-family: 'Arial'; margin: 0; padding: 0; }
            .header-title { display: flex; align-items: center; justify-content: center; margin: 25px 0 35px 0; }
            .icon-box { font-size: 28px; color: #2563EB; margin-right: 12px; font-weight: 900; }
            .main-title { font-size: 28px; font-weight: 700; color: #000; margin: 0; }

            .filter-container {
                display: flex; justify-content: center; flex-wrap: nowrap;
                gap: 25px; margin: 10px auto 40px; max-width: 1200px;
            }
            .filter-card {
                background: #fff; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.06);
                padding: 15px 18px; flex: 1 1 22%; min-width: 240px; max-width: 280px;
            }
            .filter-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.12); }
            .graph-container { display: flex; flex-direction: column; align-items: center; gap: 35px; margin: 25px 0 40px; }
            .graph-card {
                width: 90%; background: #fff; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.06);
                padding: 12px;
            }
        </style>
    </head>
    <body>
        {%app_entry%}
        <footer>{%config%}{%scripts%}{%renderer%}</footer>
    </body>
    </html>
    '''

    app.run(
        host="0.0.0.0",
        port=8050,
        debug=False,
        use_reloader=False
    )



if __name__ == "__main__":
    run_dashboard()

