"""Run a SQL file statement by statement on a Databricks SQL warehouse.

    python -m ireland_rsi.dbx_sql sql/setup.sql [--var catalog=ireland_rsi_donegal]
        [--profile ireland-rsi] [--warehouse-id ID]

${name} placeholders are replaced from --var name=value (catalog defaults to ireland_rsi).

Uses the Databricks CLI profile for authentication. Statements are split on semicolons at
line ends; keep one statement per terminating ';' and no semicolons inside string literals.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState


def split_statements(sql: str) -> list[str]:
    without_comments = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    parts = re.split(r";\s*(?:\n|$)", without_comments)
    return [p.strip() for p in parts if p.strip()]


def substitute(sql: str, variables: dict[str, str]) -> str:
    """Replace ${name} placeholders; unknown names are an error, not left in the SQL."""
    def value(match: re.Match) -> str:
        name = match.group(1)
        if name not in variables:
            raise SystemExit(f"no value for ${{{name}}}; pass --var {name}=...")
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", variables[name]):
            raise SystemExit(f"unsafe value for {name}: {variables[name]!r}")
        return variables[name]

    return re.sub(r"\$\{([a-z_]+)\}", value, sql)


def default_warehouse_id(client: WorkspaceClient) -> str:
    warehouses = list(client.warehouses.list())
    if len(warehouses) != 1:
        raise SystemExit(
            f"expected exactly one warehouse, found {len(warehouses)}; pass --warehouse-id"
        )
    return warehouses[0].id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sql_file", type=Path)
    parser.add_argument("--profile", default="ireland-rsi")
    parser.add_argument("--warehouse-id")
    parser.add_argument("--var", action="append", default=[], metavar="NAME=VALUE")
    args = parser.parse_args()
    variables = {"catalog": "ireland_rsi", **dict(v.split("=", 1) for v in args.var)}

    client = WorkspaceClient(profile=args.profile)
    warehouse_id = args.warehouse_id or default_warehouse_id(client)
    failed = False
    sql_text = substitute(args.sql_file.read_text(encoding="utf-8"), variables)
    for statement in split_statements(sql_text):
        response = client.statement_execution.execute_statement(
            statement=statement, warehouse_id=warehouse_id, wait_timeout="50s"
        )
        state = response.status.state
        first_line = statement.splitlines()[0]
        print(f"{state.value:<10} {first_line}")
        if state is not StatementState.SUCCEEDED:
            failed = True
            if response.status.error:
                print(f"           {response.status.error.message}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
