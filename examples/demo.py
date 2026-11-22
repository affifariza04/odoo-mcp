"""Launch the server over stdio (as Claude Desktop / Cursor would) and run a short agent-style session."""

import asyncio
import json
import sys

from mcp import Client, StdioServerParameters, types


async def confirm(context, params):
    print(f"\n[human is asked]\n{params.message}\n[human clicks: Confirm]")
    return types.ElicitResult(action="accept", content={"confirm": True})


async def main() -> None:
    server = StdioServerParameters(command=sys.executable, args=["-m", "odoo_mcp.server"], env={"ODOO_MCP_MODE": "readwrite"})
    async with Client(server, elicitation_callback=confirm) as c:
        tools = (await c.list_tools()).tools
        print("tools:", ", ".join(t.name for t in tools))
        for name, args in [
            ("sales_report", {"date_from": "2026-08-01", "date_to": "2026-11-30", "group_by": "customer"}),
            ("overdue_invoices", {"as_of": "2026-11-22"}),
            ("check_stock", {"only_low": True}),
            ("create_quotation", {"customer_id": 3, "lines": [{"product_id": 4, "quantity": 20}, {"product_id": 1, "quantity": 12}]}),
        ]:
            res = await c.call_tool(name, args)
            print(f"\n> {name}({json.dumps(args)})")
            print(res.content[0].text if res.is_error else json.dumps(res.structured_content, indent=2)[:900])


if __name__ == "__main__":
    asyncio.run(main())
