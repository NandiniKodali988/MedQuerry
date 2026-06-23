"""
Phase 4: Streamlit dashboard — chat interface + auto-rendered charts.

Run: streamlit run app/streamlit_app.py
"""

import json
import os
import sys

import altair as alt
import anthropic
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# Allow imports from the project root (mcp_server/, chat.py)
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import mcp_server.tools as t
from chat import TOOLS, SYSTEM, dispatch

load_dotenv()

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="MedQuerry",
    page_icon="💊",
    layout="wide",
)

st.title("💊 MedQuerry")
st.caption("Medicare Part D Drug Spending Analytics · CMS Data 2019–2023")

# ── Session state ─────────────────────────────────────────────────────────────
# Streamlit reruns the whole script on every interaction.
# st.session_state is a dict that persists across those reruns.
# We use it to store the chat history so messages don't disappear.
#
# Each message is a dict:
#   {"role": "user"|"assistant", "content": str, "chart_data": dict|None}

if "messages" not in st.session_state:
    st.session_state.messages = []

# ── Chart rendering ───────────────────────────────────────────────────────────
# After Claude calls run_sql, we capture the result and try to render it.
# Logic:
#   - If there's a column named "year" with integers → line chart (trend)
#   - Otherwise → horizontal bar chart (ranking/comparison)

def try_render_chart(data: dict) -> bool:
    """
    Tries to render an Altair chart from a run_sql result dict.
    Returns True if a chart was rendered, False if the data wasn't chart-able.
    """
    rows = data.get("rows")
    if not rows or len(rows) < 2:
        return False

    df = pd.DataFrame(rows)
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    text_cols = df.select_dtypes(exclude="number").columns.tolist()

    if not numeric_cols or not text_cols:
        return False

    y_col = numeric_cols[0]
    x_col = text_cols[0]

    # Line chart for time-series data (year column present)
    if "year" in df.columns and pd.api.types.is_integer_dtype(df["year"]):
        label_col = text_cols[0] if text_cols else None
        chart = (
            alt.Chart(df)
            .mark_line(point=True)
            .encode(
                x=alt.X("year:O", title="Year"),
                y=alt.Y(f"{y_col}:Q", title=y_col.replace("_", " ")),
                color=alt.Color(f"{label_col}:N", title=label_col) if label_col else alt.value("steelblue"),
                tooltip=list(df.columns),
            )
            .properties(height=350)
            .interactive()
        )
        st.altair_chart(chart, use_container_width=True)
        return True

    # Bar chart for rankings / comparisons
    chart = (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X(f"{y_col}:Q", title=y_col.replace("_", " ")),
            y=alt.Y(f"{x_col}:N", sort="-x", title=x_col.replace("_", " ")),
            tooltip=list(df.columns),
            color=alt.Color(f"{y_col}:Q", scale=alt.Scale(scheme="blues"), legend=None),
        )
        .properties(height=max(200, len(df) * 28))
        .interactive()
    )
    st.altair_chart(chart, use_container_width=True)
    return True


# ── Ask loop ──────────────────────────────────────────────────────────────────
# This is the same agentic loop as chat.py, adapted for Streamlit:
#   - st.status() shows a live indicator of which tools are being called
#   - We capture the last run_sql result so we can render a chart after

def ask_streamlit(question: str) -> tuple[str, dict | None]:
    """
    Runs the Claude tool-use loop and returns (answer_text, chart_data).
    chart_data is the result of the last run_sql call, or None.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    messages = [{"role": "user", "content": question}]
    last_sql_result = None

    # st.status() creates a collapsible "thinking" block in the UI.
    # Updates to it are visible in real-time as the loop runs.
    with st.status("Thinking...", expanded=True) as status:
        while True:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=SYSTEM,
                tools=TOOLS,
                messages=messages,
            )

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                status.update(label="Done", state="complete", expanded=False)
                final = next(b.text for b in response.content if hasattr(b, "text"))
                return final, last_sql_result

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue

                    # Show the tool call in the status widget
                    args_preview = json.dumps(block.input)[:80]
                    status.write(f"`{block.name}({args_preview})`")

                    result_str = dispatch(block.name, block.input)

                    # Capture run_sql results for chart rendering
                    if block.name == "run_sql":
                        last_sql_result = json.loads(result_str)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    })

                messages.append({"role": "user", "content": tool_results})


# ── Chat history display ──────────────────────────────────────────────────────
# Replay all stored messages at the top of every rerun.
# Charts are stored alongside messages so they persist too.

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("chart_data"):
            try_render_chart(msg["chart_data"])

# ── Input ─────────────────────────────────────────────────────────────────────
# st.chat_input() pins an input box to the bottom of the page.
# It returns the submitted text (or None if nothing submitted this rerun).

if prompt := st.chat_input("Ask about Medicare Part D drug spending..."):

    # Show user message immediately
    st.session_state.messages.append({"role": "user", "content": prompt, "chart_data": None})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Run the tool-use loop and show assistant response
    with st.chat_message("assistant"):
        answer, chart_data = ask_streamlit(prompt)
        st.markdown(answer)
        if chart_data:
            try_render_chart(chart_data)

    # Persist the assistant message (with chart data) so it survives reruns
    st.session_state.messages.append({
        "role": "assistant",
        "content": answer,
        "chart_data": chart_data,
    })
