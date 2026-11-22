"""The MCP server: business-level tools on top of a guarded Odoo backend."""

import json
import os
import sys
from datetime import date
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import AcceptedElicitation, Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__
from .backend import DemoBackend, OdooBackend, XmlRpcBackend
from .policy import Policy

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


# ------------------------------------------------------------------ result models (structured tool output)

class Customer(BaseModel):
    id: int
    name: str
    city: str | None = None
    email: str | None = None


class StockLine(BaseModel):
    id: int
    sku: str | None
    name: str
    on_hand: float
    price: float
    status: Literal["ok", "low", "out"]


class SalesRow(BaseModel):
    key: str = Field(description="Month (YYYY-MM), customer name or product name")
    orders: int
    revenue: float


class SalesReport(BaseModel):
    date_from: str
    date_to: str
    group_by: str
    total_revenue: float
    rows: list[SalesRow]


class Invoice(BaseModel):
    number: str
    customer: str
    due: str
    days_overdue: int
    amount_due: float


class QuoteLine(BaseModel):
    product_id: int
    quantity: float = Field(gt=0)


class Quotation(BaseModel):
    id: int
    name: str
    customer: str
    total: float
    state: Literal["draft"] = "draft"


class Confirm(BaseModel):
    confirm: bool = Field(description="Create this quotation in Odoo?")


# ------------------------------------------------------------------ server

def _name(m2o: Any) -> str:
    return m2o[1] if isinstance(m2o, list) else str(m2o)


def build_server(backend: OdooBackend, policy: Policy | None = None, low_stock: float = 15) -> MCPServer:
    policy = policy or Policy()
    mcp = MCPServer(
        "odoo",
        title="Odoo ERP",
        version=__version__,
        instructions=(
            "Tools for an Odoo ERP. Prefer the business tools (find_customers, check_stock, sales_report, "
            "overdue_invoices) over search_records. Amounts are in the company currency. "
            "create_quotation asks the human to confirm before anything is written."
        ),
    )

    @mcp.tool(annotations=READ)
    def search_records(model: str, domain: list[Any] | None = None, fields: list[str] | None = None,
                       limit: int = 20, order: str | None = None) -> list[dict[str, Any]]:
        """Generic read: Odoo search_read on an allowed model. Domain uses Odoo syntax,
        e.g. [["state","=","sale"],["amount_total",">",1000000]]."""
        policy.check_model(model)
        flds = policy.clean_fields(fields or ["name"])
        return backend.search_read(model, policy.validate_domain(domain or []), flds, policy.clamp_limit(limit), order)

    @mcp.tool(annotations=READ)
    def find_customers(query: str, limit: int = 10) -> list[Customer]:
        """Find customers by (part of) their name, email or city."""
        domain = ["|", "|", ["name", "ilike", query], ["email", "ilike", query], ["city", "ilike", query]]
        rows = backend.search_read("res.partner", domain + [["customer_rank", ">", 0]],
                                   ["name", "city", "email"], policy.clamp_limit(limit), "name asc")
        return [Customer(**{k: v or None for k, v in r.items()}) for r in rows]

    @mcp.tool(annotations=READ)
    def check_stock(query: str = "", only_low: bool = False) -> list[StockLine]:
        """On-hand quantity per product, flagged ok / low / out. Use only_low=true for a reorder list."""
        domain = ["|", ["name", "ilike", query], ["default_code", "ilike", query]] if query else []
        rows = backend.search_read("product.product", domain, ["name", "default_code", "qty_available", "list_price"],
                                   policy.max_rows, "qty_available asc")
        out = []
        for r in rows:
            q = r.get("qty_available") or 0
            status = "out" if q <= 0 else "low" if q < low_stock else "ok"
            if only_low and status == "ok":
                continue
            out.append(StockLine(id=r["id"], sku=r.get("default_code"), name=r["name"], on_hand=q,
                                 price=r.get("list_price") or 0, status=status))
        return out

    @mcp.tool(annotations=READ)
    def sales_report(date_from: str, date_to: str, group_by: Literal["month", "customer", "product"] = "month") -> SalesReport:
        """Confirmed sales between two ISO dates (inclusive), grouped by month, customer or product."""
        try:
            d0, d1 = date.fromisoformat(date_from), date.fromisoformat(date_to)
        except ValueError as e:
            raise ToolError(f"Dates must be ISO format YYYY-MM-DD ({e})") from e
        if d1 < d0:
            raise ToolError("date_to is before date_from")
        if group_by == "product":
            orders = backend.search_read("sale.order", [["state", "=", "sale"], ["date_order", ">=", date_from],
                                                        ["date_order", "<=", date_to]], ["name"], 10_000)
            ids = [o["id"] for o in orders]
            groups = backend.read_group("sale.order.line", [["order_id", "in", ids]], ["price_subtotal:sum"], "product_id")
            rows = [SalesRow(key=_name(g["product_id"]), orders=g["product_id_count"], revenue=g["price_subtotal"]) for g in groups]
        else:
            gb = "date_order:month" if group_by == "month" else "partner_id"
            groups = backend.read_group("sale.order", [["state", "=", "sale"], ["date_order", ">=", date_from],
                                                       ["date_order", "<=", date_to]], ["amount_total:sum"], gb)
            count_key = "date_order_count" if group_by == "month" else "partner_id_count"
            rows = [SalesRow(key=_name(g[gb]), orders=g[count_key], revenue=g["amount_total"]) for g in groups]
        rows.sort(key=lambda r: r.key if group_by == "month" else -r.revenue)
        return SalesReport(date_from=date_from, date_to=date_to, group_by=group_by,
                           total_revenue=round(sum(r.revenue for r in rows), 2), rows=rows)

    @mcp.tool(annotations=READ)
    def overdue_invoices(as_of: str | None = None) -> list[Invoice]:
        """Posted customer invoices past their due date that are not fully paid, most overdue first."""
        today = date.fromisoformat(as_of) if as_of else date.today()
        rows = backend.search_read(
            "account.move",
            [["move_type", "=", "out_invoice"], ["state", "=", "posted"], ["payment_state", "in", ["not_paid", "partial"]],
             ["invoice_date_due", "<", today.isoformat()]],
            ["name", "partner_id", "invoice_date_due", "amount_residual"], policy.max_rows, "invoice_date_due asc")
        return [Invoice(number=r["name"], customer=_name(r["partner_id"]), due=r["invoice_date_due"],
                        days_overdue=(today - date.fromisoformat(r["invoice_date_due"])).days,
                        amount_due=r["amount_residual"]) for r in rows]

    def _quote(customer_id: int, lines: list[QuoteLine]) -> tuple[dict[str, Any], dict[int, dict[str, Any]], float]:
        customer = backend.search_read("res.partner", [["id", "=", customer_id]], ["name"], 1)
        if not customer:
            raise ToolError(f"No customer with id {customer_id}")
        ids = [ln.product_id for ln in lines]
        products = {p["id"]: p for p in backend.search_read("product.product", [["id", "in", ids]], ["name", "list_price"], len(ids))}
        missing = [i for i in ids if i not in products]
        if missing:
            raise ToolError(f"Unknown product id(s): {missing}")
        return customer[0], products, sum(products[ln.product_id]["list_price"] * ln.quantity for ln in lines)

    def ask_confirmation(customer_id: int, lines: list[QuoteLine]) -> Elicit[Confirm]:
        """Resolver: validates the request, then asks the human (runs before the tool body)."""
        policy.check_write("sale.order")
        customer, products, total = _quote(customer_id, lines)
        summary = "\n".join(f"  {ln.quantity:g} x {products[ln.product_id]['name']}" for ln in lines)
        return Elicit(f"Create a draft quotation for {customer['name']} (total {total:,.0f})?\n{summary}", Confirm)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
    def create_quotation(
        customer_id: int,
        lines: list[QuoteLine],
        answer: Annotated[ElicitationResult[Confirm], Resolve(ask_confirmation)],
    ) -> Quotation | str:
        """Create a DRAFT sales quotation. The human sees a summary and must confirm; nothing is confirmed or sent."""
        if not isinstance(answer, AcceptedElicitation) or not answer.data.confirm:
            return "Cancelled by the user. Nothing was written to Odoo."
        customer, _, total = _quote(customer_id, lines)
        order_lines = [[0, 0, {"product_id": ln.product_id, "product_uom_qty": ln.quantity}] for ln in lines]
        oid = backend.create("sale.order", {"partner_id": customer_id, "order_line": order_lines, "state": "draft",
                                            "amount_total": total, "date_order": date.today().isoformat()})
        name = backend.search_read("sale.order", [["id", "=", oid]], ["name"], 1)[0].get("name") or f"#{oid}"
        return Quotation(id=oid, name=name, customer=customer["name"], total=total)

    @mcp.resource("odoo://models", mime_type="application/json")
    def models() -> str:
        """The Odoo models this server exposes, and whether writes are enabled."""
        return json.dumps({"mode": policy.mode, "models": sorted(policy.models), "writable": sorted(policy.writable_models)})

    @mcp.prompt()
    def month_end_review(month: str) -> str:
        """Month-end review: sales vs previous month, overdue invoices to chase, products to reorder."""
        return (f"Do a month-end review for {month} using the Odoo tools. 1) sales_report for {month} and the month before, "
                "grouped by customer; call out the biggest changes. 2) overdue_invoices: list who to chase first and the total. "
                "3) check_stock with only_low=true: propose reorder quantities. Keep it to one short table per section.")

    return mcp


def backend_from_env() -> tuple[OdooBackend, Policy]:
    mode = os.environ.get("ODOO_MCP_MODE", "readonly")
    if mode not in ("readonly", "readwrite"):
        sys.exit("ODOO_MCP_MODE must be 'readonly' or 'readwrite'")
    url = os.environ.get("ODOO_URL")
    if not url:
        print("ODOO_URL not set: running against the built-in demo company.", file=sys.stderr)
        return DemoBackend(), Policy(mode=mode)  # type: ignore[arg-type]
    backend = XmlRpcBackend(url, os.environ["ODOO_DB"], os.environ["ODOO_USER"], os.environ["ODOO_API_KEY"])
    return backend, Policy(mode=mode)  # type: ignore[arg-type]


def main() -> None:
    backend, policy = backend_from_env()
    server = build_server(backend, policy)
    if "--http" in sys.argv:
        server.run("streamable-http")
    else:
        server.run()


if __name__ == "__main__":
    main()
