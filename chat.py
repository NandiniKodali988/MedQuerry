"""
Phase 3: text-to-SQL loop via Claude tool use.

Run: python chat.py
Ask questions like:
  - Which drugs cost the most per dose in 2023?
  - How has Ozempic spending changed since 2019?
  - Compare Eliquis and Xarelto
  - Which drugs had the fastest cost growth?
"""

import json
import os
from dotenv import load_dotenv
import anthropic
import mcp_server.tools as t

load_dotenv()

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ── Tool definitions ──────────────────────────────────────────────────────────
# Same 5 tools as server.py, but in Anthropic's format.
# "description" is what Claude reads to decide which tool to call.
# "input_schema" tells Claude what arguments to pass.

TOOLS = [
    {
        "name": "get_schema",
        "description": (
            "Returns the schema and column descriptions for the CMS Medicare "
            "Part D drug spending dataset (2019–2023). Call this first before "
            "writing any SQL query."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_sql",
        "description": (
            "Executes a SQL query against the CMS dataset using DuckDB. "
            "Use read_csv_auto('...') as the table reference. "
            "Always call get_schema first to know the column names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The SQL query to execute"},
                "limit": {"type": "integer", "description": "Max rows to return (default 500)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "find_cost_outliers",
        "description": (
            "Finds drugs whose cost per dose unit is a statistical outlier "
            "(above Q3 + 1.5*IQR) for the specified year. "
            "Use for questions about expensive, unusual, or anomalous drugs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {
                    "type": "integer",
                    "description": "Year to analyze: 2019, 2020, 2021, 2022, or 2023",
                    "enum": [2019, 2020, 2021, 2022, 2023],
                }
            },
            "required": [],
        },
    },
    {
        "name": "summarize_trends",
        "description": (
            "Returns year-by-year spend, beneficiary count, and cost-per-unit "
            "for a single drug (2019–2023). Use for questions about a specific "
            "drug's trajectory or growth."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "drug_name": {
                    "type": "string",
                    "description": "Brand name of the drug (e.g. 'Ozempic', 'Eliquis')",
                }
            },
            "required": ["drug_name"],
        },
    },
    {
        "name": "compare_drugs",
        "description": (
            "Side-by-side comparison of two drugs across all years (2019–2023): "
            "spend, beneficiaries, cost per unit. Use for head-to-head questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "drug_a": {"type": "string", "description": "First drug brand name"},
                "drug_b": {"type": "string", "description": "Second drug brand name"},
            },
            "required": ["drug_a", "drug_b"],
        },
    },
]

# ── Tool dispatcher ───────────────────────────────────────────────────────────
# Routes a tool call from Claude to the right function in tools.py.

def dispatch(tool_name: str, tool_input: dict) -> str:
    if tool_name == "get_schema":
        result = t.get_schema()
    elif tool_name == "run_sql":
        result = t.run_sql(tool_input["query"], tool_input.get("limit", 500))
    elif tool_name == "find_cost_outliers":
        result = t.find_cost_outliers(tool_input.get("year", 2023))
    elif tool_name == "summarize_trends":
        result = t.summarize_trends(tool_input["drug_name"])
    elif tool_name == "compare_drugs":
        result = t.compare_drugs(tool_input["drug_a"], tool_input["drug_b"])
    else:
        result = {"error": f"Unknown tool: {tool_name}"}
    return json.dumps(result)

# ── Agentic loop ──────────────────────────────────────────────────────────────
# This is the core pattern. We keep sending messages until Claude stops
# requesting tool calls (stop_reason == "end_turn").

SYSTEM = (
    "You are a healthcare data analyst with access to CMS Medicare Part D "
    "drug spending data (2019–2023). Answer questions clearly and concisely. "
    "Always call get_schema before writing SQL. "
    "When presenting numbers, format dollars with $ and use B/M suffixes for billions/millions."
)

def ask(question: str, verbose: bool = True) -> str:
    messages = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        # Add Claude's response to the conversation history.
        # This is required — Claude needs to see its own prior messages.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Claude is done. Extract the final text response.
            final = next(b.text for b in response.content if hasattr(b, "text"))
            return final

        if response.stop_reason == "tool_use":
            # Claude wants to call one or more tools.
            # Collect all tool results before sending them back.
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                if verbose:
                    print(f"  [tool] {block.name}({json.dumps(block.input)})")

                result_str = dispatch(block.name, block.input)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,  # must match the id Claude sent
                    "content": result_str,
                })

            # Send all tool results back in a single user message.
            messages.append({"role": "user", "content": tool_results})

# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("MedQuerry — ask anything about Medicare Part D drug spending (2019–2023)")
    print("Type 'quit' to exit.\n")

    while True:
        question = input("You: ").strip()
        if question.lower() in ("quit", "exit", "q"):
            break
        if not question:
            continue

        print()
        answer = ask(question, verbose=True)
        print(f"\nClaude: {answer}\n")
        print("-" * 60)
