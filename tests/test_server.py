"""End-to-end through a real MCP client connected in-process, exactly as an agent would call the tools."""

import json

import pytest
from mcp import Client, types

from odoo_mcp.backend import DemoBackend
from odoo_mcp.policy import Policy
from odoo_mcp.server import build_server


def accept(confirm: bool):
    async def cb(context, params):
        return types.ElicitResult(action="accept", content={"confirm": confirm})
    return cb


async def decline_cb(context, params):
    return types.ElicitResult(action="decline")


async def call(client, name, args=None):
    res = await client.call_tool(name, args or {})
    assert not res.is_error, res.content
    return res.structured_content


async def test_lists_tools_with_read_only_hints():
    async with Client(build_server(DemoBackend())) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert {"search_records", "find_customers", "check_stock", "sales_report", "overdue_invoices", "create_quotation"} <= tools.keys()
    assert tools["sales_report"].annotations.read_only_hint is True
    assert tools["create_quotation"].annotations.read_only_hint is False


async def test_business_tools_on_demo_company():
    async with Client(build_server(DemoBackend())) as c:
        customers = (await call(c, "find_customers", {"query": "jakarta"}))["result"]
        assert {x["name"] for x in customers} == {"Kopi Senja Cafe", "Ruang Temu Co-working", "PT Sinar Retail"}

        low = (await call(c, "check_stock", {"only_low": True}))["result"]
        assert [(x["name"], x["status"]) for x in low] == [
            ("Cold Brew Bottle 250ml", "out"), ("Kintamani Natural 1kg", "low"), ("Toraja Kalosi 250g", "low")]

        report = await call(c, "sales_report", {"date_from": "2026-09-01", "date_to": "2026-09-30", "group_by": "month"})
        assert report["rows"] == [{"key": "2026-09", "orders": 2, "revenue": 17_300_000.0}]  # cancelled order excluded

        overdue = (await call(c, "overdue_invoices", {"as_of": "2026-11-22"}))["result"]
        assert [(i["number"], i["days_overdue"]) for i in overdue] == [("INV/2026/0004", 51), ("INV/2026/0008", 6)]


async def test_policy_errors_reach_the_model_as_tool_errors():
    async with Client(build_server(DemoBackend())) as c:
        res = await c.call_tool("search_records", {"model": "res.users", "fields": ["login"]})
        assert res.is_error and "not exposed" in res.content[0].text
        res = await c.call_tool("search_records", {"model": "res.partner", "fields": ["password"]})
        assert res.is_error and "not readable" in res.content[0].text


async def test_readonly_mode_refuses_writes():
    async with Client(build_server(DemoBackend()), elicitation_callback=accept(True)) as c:
        res = await c.call_tool("create_quotation", {"customer_id": 1, "lines": [{"product_id": 4, "quantity": 5}]})
    assert res.is_error and "read-only" in res.content[0].text


@pytest.mark.parametrize("callback, created", [(accept(True), True), (accept(False), False), (decline_cb, False)])
async def test_quotation_requires_human_confirmation(callback, created):
    backend = DemoBackend()
    before = len(backend.db["sale.order"])
    async with Client(build_server(backend, Policy(mode="readwrite")), elicitation_callback=callback) as c:
        res = await c.call_tool("create_quotation", {"customer_id": 1, "lines": [{"product_id": 4, "quantity": 5}]})
    assert not res.is_error
    if not created:
        assert "Nothing was written" in res.content[0].text
    assert len(backend.db["sale.order"]) == before + (1 if created else 0)
    if created:
        q = res.structured_content["result"]
        assert q["total"] == 1_550_000 and q["state"] == "draft" and q["name"] == "S00013"


async def test_models_resource():
    async with Client(build_server(DemoBackend())) as c:
        res = await c.read_resource("odoo://models")
    data = json.loads(res.contents[0].text)
    assert data["mode"] == "readonly" and "sale.order" in data["models"]
