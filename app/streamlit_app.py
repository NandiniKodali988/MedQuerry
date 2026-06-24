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

# ── Chart data extraction ─────────────────────────────────────────────────────
# Each tool returns a different structure. This normalises them all to a flat
# list of row-dicts that try_render_chart can work with.

def extract_chart_rows(tool_name: str, result: dict) -> list[dict] | None:
    if tool_name == "run_sql":
        return result.get("rows")

    if tool_name in ("find_cost_outliers",):
        return result.get("rows")

    if tool_name == "summarize_trends":
        # result has a "trend" key: [{year, total_spend_millions, ...}, ...]
        # Add the drug name as a column so the chart has a label.
        rows = result.get("trend")
        if rows and result.get("drug"):
            return [{**r, "drug": result["drug"]} for r in rows]
        return rows

    if tool_name == "compare_drugs":
        # result is {drug_a: {drug, trend: [...]}, drug_b: {...}}
        # Flatten both trends into one list with a "drug" column for color.
        rows = []
        for entry in result.values():
            if isinstance(entry, dict) and "trend" in entry:
                for r in entry["trend"]:
                    rows.append({**r, "drug": entry.get("drug", "")})
        return rows or None

    return None


# ── Chart rendering ───────────────────────────────────────────────────────────
# Two chart types:
#   - "year" column present → line chart (trend over time)
#   - Otherwise            → horizontal bar chart (ranking / comparison)
#
# Key fix vs original: we explicitly coerce columns to numeric with
# pd.to_numeric(errors="ignore") before calling select_dtypes. Without this,
# columns that have a single None mixed with floats stay as object dtype and
# get missed by select_dtypes(include="number").

def try_render_chart(data: dict) -> bool:
    try:
        rows = data.get("rows")
        if not rows or len(rows) < 2:
            return False

        df = pd.DataFrame(rows)

        # Coerce every column to numeric where possible — fixes the None issue.
        for col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="ignore")

        numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        text_cols = [c for c in df.columns if not pd.api.types.is_numeric_dtype(df[c])]

        if not numeric_cols:
            return False

        # ── Line chart: year on x-axis, first non-year numeric on y ──────────
        if "year" in df.columns:
            value_cols = [c for c in numeric_cols if c != "year"]
            if not value_cols:
                return False
            y_col = value_cols[0]
            color_col = next((c for c in text_cols if c not in ("drug",)), None) or (
                "drug" if "drug" in text_cols else None
            )
            encode = dict(
                x=alt.X("year:O", title="Year"),
                y=alt.Y(f"{y_col}:Q", title=y_col.replace("_", " ")),
                tooltip=[c for c in df.columns if c in numeric_cols + text_cols],
            )
            if color_col:
                encode["color"] = alt.Color(f"{color_col}:N", title=color_col)
            chart = (
                alt.Chart(df).mark_line(point=True).encode(**encode)
                .properties(height=350).interactive()
            )
            st.altair_chart(chart, use_container_width=True)
            return True

        # ── Bar chart: first text col on y, first numeric on x ───────────────
        if not text_cols:
            return False
        x_col = text_cols[0]
        y_col = numeric_cols[0]
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

    except Exception:
        return False


# ── Ask loop ──────────────────────────────────────────────────────────────────
# This is the same agentic loop as chat.py, adapted for Streamlit:
#   - st.status() shows a live indicator of which tools are being called
#   - We capture the last run_sql result so we can render a chart after

CHARTABLE_TOOLS = {"run_sql", "find_cost_outliers", "summarize_trends", "compare_drugs"}


def ask_streamlit(question: str) -> tuple[str, dict | None]:
    """
    Runs the Claude tool-use loop and returns (answer_text, chart_data).
    chart_data is {"rows": [...]} from the last chartable tool call, or None.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    messages = [{"role": "user", "content": question}]
    last_chart_data = None

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
                return final, last_chart_data

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue

                    args_preview = json.dumps(block.input)[:80]
                    status.write(f"`{block.name}({args_preview})`")

                    result_str = dispatch(block.name, block.input)

                    # Normalise any chartable tool result to {"rows": [...]}
                    if block.name in CHARTABLE_TOOLS:
                        result_dict = json.loads(result_str)
                        rows = extract_chart_rows(block.name, result_dict)
                        if rows:
                            last_chart_data = {"rows": rows}

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
