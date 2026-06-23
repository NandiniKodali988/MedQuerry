import os
import duckdb

DATA_PATH = os.getenv("DATA_PATH", "data/medicare_part_d_spending.csv")

# One shared connection — DuckDB is not a server, just a library linked into
# this process. A single connection is fine for our single-process use case.
_con = duckdb.connect()

YEARS = [2019, 2020, 2021, 2022, 2023]


def get_schema() -> dict:
    """
    Returns a description of the dataset so Claude can write accurate SQL.
    Called first in every text-to-SQL interaction.
    """
    year_cols = "\n".join(
        f"  - Tot_Spndng_{y}: total Medicare spend (USD)\n"
        f"  - Tot_Benes_{y}: number of beneficiaries\n"
        f"  - Tot_Clms_{y}: number of prescription claims\n"
        f"  - Avg_Spnd_Per_Dsg_Unt_Wghtd_{y}: weighted avg cost per dose unit\n"
        f"  - Avg_Spnd_Per_Bene_{y}: avg spend per beneficiary\n"
        f"  - Outlier_Flag_{y}: 1 if CMS flagged this drug as a cost outlier"
        for y in YEARS
    )

    return {
        "table": f"read_csv_auto('{DATA_PATH}')",
        "note": (
            "Filter to Mftr_Name = 'Overall' for drug-level totals. "
            "Other rows are per-manufacturer breakdowns of the same drug."
        ),
        "columns": {
            "Brnd_Name": "Brand name of the drug (e.g. 'Eliquis', 'Ozempic')",
            "Gnrc_Name": "Generic/chemical name (e.g. 'Apixaban', 'Semaglutide')",
            "Mftr_Name": "Manufacturer name, or 'Overall' for the drug-level aggregate",
            "Tot_Mftr": "Number of manufacturers for this drug",
            "Chg_Avg_Spnd_Per_Dsg_Unt_22_23": "Year-over-year change in cost per dose unit, 2022→2023",
            "CAGR_Avg_Spnd_Per_Dsg_Unt_19_23": "5-year compound annual growth rate of cost per dose unit",
        },
        "per_year_columns": year_cols,
        "example_queries": [
            "SELECT Brnd_Name, Tot_Spndng_2023 FROM read_csv_auto('...') WHERE Mftr_Name='Overall' ORDER BY Tot_Spndng_2023 DESC LIMIT 10",
            "SELECT Brnd_Name, CAGR_Avg_Spnd_Per_Dsg_Unt_19_23 FROM read_csv_auto('...') WHERE Mftr_Name='Overall' AND CAGR_Avg_Spnd_Per_Dsg_Unt_19_23 > 0.2",
        ],
    }


def run_sql(query: str, limit: int = 500) -> dict:
    """
    Executes a SQL query against the CMS dataset and returns results as a list
    of dicts. Claude writes the SQL; this function just runs it.

    The query should use read_csv_auto('...') as the table reference —
    we substitute the real path before executing.
    """
    query = query.replace("read_csv_auto('...')", f"read_csv_auto('{DATA_PATH}')")

    try:
        rel = _con.execute(query)
        cols = [desc[0] for desc in rel.description]
        rows = rel.fetchmany(limit)
        return {
            "columns": cols,
            "rows": [dict(zip(cols, row)) for row in rows],
            "row_count": len(rows),
            "truncated": len(rows) == limit,
        }
    except Exception as e:
        return {"error": str(e)}


def find_cost_outliers(year: int = 2023) -> dict:
    """
    Finds drugs whose per-unit cost is an outlier for a given year using IQR
    (interquartile range): outlier = cost > Q3 + 1.5 * IQR.
    Returns only Overall rows (drug-level, not per-manufacturer).
    """
    if year not in YEARS:
        return {"error": f"Year must be one of {YEARS}"}

    col = f"Avg_Spnd_Per_Dsg_Unt_Wghtd_{year}"
    spend_col = f"Tot_Spndng_{year}"
    benes_col = f"Tot_Benes_{year}"

    query = f"""
        WITH stats AS (
            SELECT
                PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY {col}) AS q1,
                PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY {col}) AS q3
            FROM read_csv_auto('{DATA_PATH}')
            WHERE Mftr_Name = 'Overall' AND {col} IS NOT NULL
        )
        SELECT
            d.Brnd_Name,
            d.Gnrc_Name,
            ROUND(d.{col}, 2)       AS cost_per_unit,
            ROUND(d.{spend_col}/1e6, 2) AS total_spend_millions,
            d.{benes_col}           AS beneficiaries,
            ROUND(s.q3 + 1.5*(s.q3 - s.q1), 2) AS outlier_threshold
        FROM read_csv_auto('{DATA_PATH}') d, stats s
        WHERE d.Mftr_Name = 'Overall'
          AND d.{col} > s.q3 + 1.5*(s.q3 - s.q1)
        ORDER BY d.{col} DESC
        LIMIT 50
    """
    try:
        rel = _con.execute(query)
        cols = [desc[0] for desc in rel.description]
        rows = rel.fetchall()
        return {
            "year": year,
            "method": "IQR: cost > Q3 + 1.5 * IQR",
            "columns": cols,
            "rows": [dict(zip(cols, row)) for row in rows],
            "outlier_count": len(rows),
        }
    except Exception as e:
        return {"error": str(e)}


def summarize_trends(drug_name: str) -> dict:
    """
    Returns year-by-year spend, beneficiary count, and cost-per-unit for a
    single drug (matched on brand name, case-insensitive).
    Pivots the wide format into a readable time series.
    """
    query = f"""
        SELECT *
        FROM read_csv_auto('{DATA_PATH}')
        WHERE LOWER(Brnd_Name) = LOWER('{drug_name}')
          AND Mftr_Name = 'Overall'
        LIMIT 1
    """
    try:
        rel = _con.execute(query)
        cols = [desc[0] for desc in rel.description]
        row = rel.fetchone()
        if row is None:
            return {"error": f"Drug '{drug_name}' not found. Check Brnd_Name spelling."}

        record = dict(zip(cols, row))
        trend = []
        for y in YEARS:
            trend.append({
                "year": y,
                "total_spend_millions": round((record.get(f"Tot_Spndng_{y}") or 0) / 1e6, 2),
                "beneficiaries": record.get(f"Tot_Benes_{y}"),
                "claims": record.get(f"Tot_Clms_{y}"),
                "avg_cost_per_unit": round(record.get(f"Avg_Spnd_Per_Dsg_Unt_Wghtd_{y}") or 0, 4),
                "avg_cost_per_beneficiary": round(record.get(f"Avg_Spnd_Per_Bene_{y}") or 0, 2),
                "outlier_flag": record.get(f"Outlier_Flag_{y}"),
            })

        return {
            "drug": record["Brnd_Name"],
            "generic": record["Gnrc_Name"],
            "cagr_cost_per_unit_19_23": record.get("CAGR_Avg_Spnd_Per_Dsg_Unt_19_23"),
            "trend": trend,
        }
    except Exception as e:
        return {"error": str(e)}


def compare_drugs(drug_a: str, drug_b: str) -> dict:
    """
    Side-by-side comparison of two drugs across all years.
    Returns spend, beneficiaries, and cost-per-unit for each.
    """
    results = {}
    for name in [drug_a, drug_b]:
        results[name] = summarize_trends(name)
    return results
