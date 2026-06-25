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

# ── Sidebar ───────────────────────────────────────────────────────────────────

THEMES = {
    "Default":          "default",
    "Dark":             "dark",
    "FiveThirtyEight":  "fivethirtyeight",
    "ggplot2":          "ggplot2",
    "Google Charts":    "googlecharts",
    "Quartz":           "quartz",
    "Vox":              "vox",
    "Urban Institute":  "urbaninstitute",
}

with st.sidebar:
    st.header("Settings")
    chosen_theme = st.selectbox(
        "Chart theme",
        options=list(THEMES.keys()),
        index=0,
    )
    st.caption("Spending unit")
    unit_label = st.radio(
        "Spending unit",
        options=["K", "M", "B"],
        index=2,
        horizontal=True,
        label_visibility="collapsed",
    )

active_theme = THEMES[chosen_theme]

# ── Session state ─────────────────────────────────────────────────────────────
# Streamlit reruns the whole script on every interaction.
# st.session_state is a dict that persists across those reruns.
# We use it to store the chat history so messages don't disappear.
#
# Each message is a dict:
#   {"role": "user"|"assistant", "content": str, "chart_data": dict|None}

if "messages" not in st.session_state:
    st.session_state.messages = []
if "dashboard" not in st.session_state:
    st.session_state.dashboard = []

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


# ── Theme application ─────────────────────────────────────────────────────────
# In Altair 5.5+, alt.themes.enable() only sets usermeta.embedOptions.theme,
# which Streamlit does not forward to Vega-Embed. Instead we apply config
# directly via .configure_*() so it is baked into the Vega-Lite spec.
# We also pass theme=None to st.altair_chart to stop Streamlit's own
# theme override from clobbering our config.

_THEME_CONFIGS = {
    "default": dict(),
    "dark": dict(
        background="#333333",
        axis=alt.AxisConfig(labelColor="#ffffff", titleColor="#ffffff",
                            gridColor="#555555", domainColor="#888888", tickColor="#888888"),
        legend=alt.LegendConfig(labelColor="#ffffff", titleColor="#ffffff"),
        title=alt.TitleConfig(color="#ffffff"),
        view=alt.ViewConfig(fill="#333333", stroke="#555555"),
        mark=alt.MarkConfig(color="#4c9be8"),
    ),
    "fivethirtyeight": dict(
        background="#F0F0F0",
        axis=alt.AxisConfig(labelColor="#5C5C5C", titleColor="#5C5C5C",
                            gridColor="#CBCBCB", domainColor="#CBCBCB"),
        view=alt.ViewConfig(fill="#F0F0F0"),
        mark=alt.MarkConfig(color="#30a2da"),
    ),
    "ggplot2": dict(
        background="#E5E5E5",
        axis=alt.AxisConfig(labelColor="#555555", titleColor="#555555",
                            gridColor="#FFFFFF", domainColor="#555555"),
        view=alt.ViewConfig(fill="#E5E5E5", stroke="transparent"),
        mark=alt.MarkConfig(color="#F8766D"),
    ),
    "googlecharts": dict(
        background="#ffffff",
        axis=alt.AxisConfig(labelColor="#757575", titleColor="#757575",
                            gridColor="#E0E0E0", domainColor="#BDBDBD"),
        view=alt.ViewConfig(fill="#ffffff"),
        mark=alt.MarkConfig(color="#3366CC"),
    ),
    "quartz": dict(
        background="#ffffff",
        axis=alt.AxisConfig(labelColor="#525252", titleColor="#525252",
                            gridColor="#E8E8E8", domainColor="#C8C8C8"),
        view=alt.ViewConfig(fill="#ffffff"),
        mark=alt.MarkConfig(color="#ab5787"),
    ),
    "vox": dict(
        background="#ffffff",
        axis=alt.AxisConfig(labelColor="#666666", titleColor="#333333",
                            gridColor="#E5E5E5", domainColor="#AAAAAA"),
        view=alt.ViewConfig(fill="#ffffff"),
        mark=alt.MarkConfig(color="#4889AB"),
    ),
    "urbaninstitute": dict(
        background="#ffffff",
        axis=alt.AxisConfig(labelColor="#1696d2", titleColor="#1696d2",
                            gridColor="#DEDDDD", domainColor="#DEDDDD"),
        view=alt.ViewConfig(fill="#ffffff"),
        mark=alt.MarkConfig(color="#1696d2"),
    ),
}

def _themed(chart, theme: str):
    cfg = _THEME_CONFIGS.get(theme, {})
    if not cfg:
        return chart
    return chart.configure(**cfg)


# ── Unit scaling ──────────────────────────────────────────────────────────────
# Spending columns are scaled to K / M / B before charting so the axis shows
# readable numbers. Per-unit and per-beneficiary cost columns are left alone —
# they have a completely different magnitude (dollars per dose, not billions).

_UNIT_FACTOR = {"K": 1e3, "M": 1e6, "B": 1e9}
_SPEND_KEYWORDS = ("total_spend", "tot_spndng", "spndng")

def _is_spend_col(col: str) -> bool:
    c = col.lower()
    # Match spending totals; exclude per-unit / per-beneficiary cost columns
    return any(k in c for k in _SPEND_KEYWORDS) and "per" not in c

def _apply_unit(df: pd.DataFrame, col: str, unit: str) -> tuple[pd.DataFrame, str]:
    """Scale a spending column to the target unit and rename it."""
    factor = _UNIT_FACTOR[unit]
    df = df.copy()
    # Columns named *_millions are already divided by 1e6 — convert back to raw first
    base = 1e6 if "_millions" in col.lower() else 1.0
    df[col] = df[col] * base / factor
    new_name = col.replace("_millions", "").rstrip("_") + f"_{unit.lower()}"
    return df.rename(columns={col: new_name}), new_name


# ── Chart rendering ───────────────────────────────────────────────────────────
# Two chart types:
#   - "year" column present → line chart (trend over time)
#   - Otherwise            → horizontal bar chart (ranking / comparison)

def try_render_chart(data: dict, theme: str = "default", unit: str = "B") -> bool:
    try:
        rows = data.get("rows")
        if not rows or len(rows) < 2:
            return False

        df = pd.DataFrame(rows)
        numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        text_cols = [c for c in df.columns if not pd.api.types.is_numeric_dtype(df[c])]

        if not numeric_cols:
            return False

        # ── Line chart ────────────────────────────────────────────────────────
        if "year" in df.columns:
            value_cols = [c for c in numeric_cols if c != "year"]
            if not value_cols:
                return False
            y_col = value_cols[0]
            if _is_spend_col(y_col):
                old = y_col
                df, y_col = _apply_unit(df, y_col, unit)
                numeric_cols = [y_col if c == old else c for c in numeric_cols]
            color_col = next((c for c in text_cols if c not in ("drug",)), None) or (
                "drug" if "drug" in text_cols else None
            )
            encode = dict(
                x=alt.X("year:O", title="Year"),
                y=alt.Y(f"{y_col}:Q", title=y_col.replace("_", " ").upper()),
                tooltip=[c for c in df.columns if c in numeric_cols + text_cols],
            )
            if color_col:
                encode["color"] = alt.Color(f"{color_col}:N", title=color_col)
            chart = (
                alt.Chart(df).mark_line(point=True).encode(**encode)
                .properties(height=350).interactive()
            )
            st.altair_chart(_themed(chart, theme), use_container_width=True, theme=None)
            return True

        # ── Bar chart ─────────────────────────────────────────────────────────
        if not text_cols:
            return False
        x_col = text_cols[0]
        y_col = numeric_cols[0]
        if _is_spend_col(y_col):
            df, y_col = _apply_unit(df, y_col, unit)
        chart = (
            alt.Chart(df)
            .mark_bar()
            .encode(
                x=alt.X(f"{y_col}:Q", title=y_col.replace("_", " ").upper()),
                y=alt.Y(f"{x_col}:N", sort="-x", title=x_col.replace("_", " ")),
                tooltip=list(df.columns),
                color=alt.Color(f"{y_col}:Q", scale=alt.Scale(scheme="blues"), legend=None),
            )
            .properties(height=max(200, len(df) * 28))
            .interactive()
        )
        st.altair_chart(_themed(chart, theme), use_container_width=True, theme=None)
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


# ── Tabs ──────────────────────────────────────────────────────────────────────

chat_tab, dashboard_tab = st.tabs(["💬 Chat", "📊 Dashboard"])

# ── Chat tab ──────────────────────────────────────────────────────────────────

with chat_tab:
    # Replay stored messages. Track the last user question so pin buttons can
    # label the chart with the question that produced it.
    last_question = ""
    for i, msg in enumerate(st.session_state.messages):
        if msg["role"] == "user":
            last_question = msg["content"]
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("chart_data"):
                rendered = try_render_chart(msg["chart_data"], theme=active_theme, unit=unit_label)
                if rendered:
                    label = last_question[:80] + ("…" if len(last_question) > 80 else "")
                    if st.button("📌 Pin to dashboard", key=f"pin_{i}"):
                        st.session_state.dashboard.append({
                            "label": label,
                            "chart_data": msg["chart_data"],
                        })
                        st.toast("Chart pinned to dashboard!")

    if prompt := st.chat_input("Ask about Medicare Part D drug spending..."):

        st.session_state.messages.append({"role": "user", "content": prompt, "chart_data": None})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            answer, chart_data = ask_streamlit(prompt)
            st.markdown(answer)
            if chart_data:
                rendered = try_render_chart(chart_data, theme=active_theme, unit=unit_label)
                if rendered:
                    label = prompt[:80] + ("…" if len(prompt) > 80 else "")
                    if st.button("📌 Pin to dashboard", key="pin_new"):
                        st.session_state.dashboard.append({
                            "label": label,
                            "chart_data": chart_data,
                        })
                        st.toast("Chart pinned to dashboard!")

        st.session_state.messages.append({
            "role": "assistant",
            "content": answer,
            "chart_data": chart_data,
        })

# ── Dashboard tab ─────────────────────────────────────────────────────────────

with dashboard_tab:
    if not st.session_state.dashboard:
        st.info("No charts pinned yet — ask a question in Chat, then click **📌 Pin to dashboard** under any chart.")
    else:
        # Render pinned charts in a 2-column grid.
        # Deletion uses st.rerun() so indices stay consistent after removal.
        to_remove = None
        cols = st.columns(2)
        for i, pinned in enumerate(st.session_state.dashboard):
            with cols[i % 2]:
                st.caption(pinned["label"])
                try_render_chart(pinned["chart_data"], theme=active_theme, unit=unit_label)
                if st.button("🗑️ Remove", key=f"remove_{i}"):
                    to_remove = i
        if to_remove is not None:
            st.session_state.dashboard.pop(to_remove)
            st.rerun()
