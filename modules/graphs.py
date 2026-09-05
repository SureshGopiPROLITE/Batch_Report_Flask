import plotly.express as px
import plotly.graph_objects as go
import base64
import pandas as pd
import plotly.io as pio

# from dash import Dash, dcc, html, Input, Output

import plotly.express as px



from datetime import datetime
def plot_error_kg(df):
    if df is None or df.empty:
        return px.bar(title="No data available")

    fig = px.bar(
        df,
        x="Category",
        y="Error_Kg",
        title="Total Loss (Kg) per Silo",
        text_auto=True,
        color="Category"
    )
    fig.update_layout(
        xaxis_title="Silo",
        yaxis_title="Error (Kg)",
        title_x=0.5,
        template="plotly_white"
    )
    return fig


def plot_error_percent(df):
    if df is None or df.empty:
        return px.bar(title="No data available")

    fig = px.bar(
        df,
        x="Category",
        y="Error_%",
        title="Total Loss (%) per Silo",
        text_auto=True,
        color="Category"
    )
    fig.update_layout(
        xaxis_title="Silo",
        yaxis_title="Error (%)",
        title_x=0.5,
        template="plotly_white"
    )
    return fig

def report_graph(df):
    try:
        print("DF completed")
        fig = px.line(df, x="TimeStamp", y="Value", color="Name", markers=True)
        graph_image_path = "graph.png"
        pio.write_image(fig, graph_image_path, format='png')
        with open(graph_image_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            encoded_image = f"data:image/png;base64,{encoded_string}"
        return encoded_image
    except Exception as e:
        print(f"Error: {e}")



def plot_error_kg(df):
    fig = px.bar(
        df,
        x="Category",
        y="Error_Kg",
        title="Extraction Error (Kg)",
        text="Error_Kg"
    )
    fig.update_traces(marker_color="steelblue", textposition="outside")

    fig.update_layout(
        autosize=True,
        xaxis_tickangle=-45,
        margin=dict(l=20, r=20, t=50, b=80),
        uniformtext_minsize=10,
        uniformtext_mode="hide",
        height=None,  # let it auto-fit
        width=None
    )
    return fig


def plot_error_percent(df):
    fig = px.bar(
        df,
        x="Category",
        y="Error_%",
        title="Extraction Error (%)",
        text="Error_%"
    )
    fig.update_traces(marker_color="steelblue", textposition="outside")

    fig.update_layout(
        autosize=True,
        xaxis_tickangle=-45,
        margin=dict(l=20, r=20, t=50, b=80),
        uniformtext_minsize=10,
        uniformtext_mode="hide",
        height=None,
        width=None
    )
    return fig


def show_bar(engineConRead):
    try:
        sql = """
            SELECT TimeStamp, BatchNo
            FROM plc_data
            WHERE TimeStamp >= datetime('now', '-6 days')
            GROUP BY BatchNo;
            """
        df_bargh = pd.read_sql_query(sql, engineConRead)
        df = df_bargh
        
        df['TimeStamp'] = pd.to_datetime(df['TimeStamp'], errors='coerce')

        # Set the TimeStamp column as the index
        df.set_index('TimeStamp', inplace=True)

        # Resample by day and count the number of entries for each day
        daily_counts = df.resample('D').size()

        # Convert the resampled data into a DataFrame for plotting
        daily_counts_df = daily_counts.reset_index(name='count')

        # Create the bar graph using Plotly
        fig = px.bar(daily_counts_df, x='TimeStamp', y='count', title="Count of Batch's per Day")

        return fig
   
    except Exception as e:
        print(f"Error: {e}")




def show_speed(Total_seconds):
    try:
        # Convert to float safely
        value = float(Total_seconds)
        
        # Set dynamic max for gauge (at least 10% higher than value)
        max_range = max(value * 1.1, 3)

        fig = go.Figure(go.Indicator(
            mode="gauge+number",
            value=value,
            domain={'x': [0, 1], 'y': [0, 1]},
            title={'text': f"Data Rate: {value:.2f}/sec"},
            gauge={
                'axis': {'range': [0, max_range], 'tickwidth': 1, 'tickcolor': "darkblue"},
                'bar': {'color': "darkblue"},
                'bgcolor': "white",
                'borderwidth': 2,
                'bordercolor': "gray",
                'threshold': {
                    'line': {'color': "red", 'width': 4},
                    'thickness': 0.75,
                    'value': max_range * 0.5  # threshold at 50% of max
                }
            }
        ))
        
        fig.update_layout(
            paper_bgcolor="#ECF1F7",
            font={'color': "#171725", 'family': "Black"}
        )
        return fig

    except Exception as e:
        print(f"❌ Error in show_speed: {e}")
        # Always return a dummy figure so caller won't crash
        return go.Figure()
    	    















