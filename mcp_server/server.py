import asyncio
import json
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

import mcp_server.tools as t

app = Server("medquerry")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    """
    MCP requires a list_tools handler that tells the client (Claude) what tools
    are available, their descriptions, and their input schemas.
    Claude reads these descriptions to decide which tool to call.
    """
    return [
        types.Tool(
            name="get_schema",
            description=(
                "Returns the schema and column descriptions for the CMS Medicare "
                "Part D drug spending dataset (2019–2023). Call this first before "
                "writing any SQL query."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="run_sql",
            description=(
                "Executes a SQL query against the CMS dataset using DuckDB. "
                "Use read_csv_auto('...') as the table reference — the real path "
                "is substituted automatically. Returns rows as JSON. "
                "Always call get_schema first to know the column names."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The SQL query to execute"},
                    "limit": {"type": "integer", "description": "Max rows to return (default 500)"},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="find_cost_outliers",
            description=(
                "Finds drugs whose cost per dose unit is a statistical outlier "
                "(above Q3 + 1.5*IQR) for the specified year. "
                "Use this for questions about expensive, unusual, or anomalous drugs."
            ),
            inputSchema={
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
        ),
        types.Tool(
            name="summarize_trends",
            description=(
                "Returns year-by-year spend, beneficiary count, and cost-per-unit "
                "for a single drug (2019–2023). Use for questions about a specific "
                "drug's trajectory or growth."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "drug_name": {
                        "type": "string",
                        "description": "Brand name of the drug (e.g. 'Ozempic', 'Eliquis')",
                    }
                },
                "required": ["drug_name"],
            },
        ),
        types.Tool(
            name="compare_drugs",
            description=(
                "Side-by-side comparison of two drugs across all years (2019–2023): "
                "spend, beneficiaries, cost per unit. Use for head-to-head questions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "drug_a": {"type": "string", "description": "First drug brand name"},
                    "drug_b": {"type": "string", "description": "Second drug brand name"},
                },
                "required": ["drug_a", "drug_b"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """
    Routes an incoming tool call to the right function in tools.py and
    returns the result as a JSON string wrapped in TextContent.
    MCP requires the return type to be a list of content blocks.
    """
    if name == "get_schema":
        result = t.get_schema()
    elif name == "run_sql":
        result = t.run_sql(arguments["query"], arguments.get("limit", 500))
    elif name == "find_cost_outliers":
        result = t.find_cost_outliers(arguments.get("year", 2023))
    elif name == "summarize_trends":
        result = t.summarize_trends(arguments["drug_name"])
    elif name == "compare_drugs":
        result = t.compare_drugs(arguments["drug_a"], arguments["drug_b"])
    else:
        result = {"error": f"Unknown tool: {name}"}

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
