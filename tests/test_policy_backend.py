import pytest

from odoo_mcp.backend import DemoBackend, matches
from odoo_mcp.policy import Policy, PolicyError

REC = {"id": 1, "name": "Kopi Senja", "city": "Jakarta", "amount": 500, "partner_id": [7, "Acme"]}


@pytest.mark.parametrize("domain, expected", [
    ([], True),
    ([["city", "=", "Jakarta"]], True),
    ([["name", "ilike", "senja"]], True),
    ([["amount", ">", 600]], False),
    ([["city", "=", "Bandung"], ["amount", ">", 100]], False),          # implicit AND
    (["|", ["city", "=", "Bandung"], ["amount", ">", 100]], True),      # OR
    (["!", ["city", "=", "Jakarta"]], False),                           # NOT
    ([["partner_id", "=", 7]], True),                                   # many2one compares by id
    ([["missing", ">", 3]], False),                                     # None vs number is just False
])
def test_domain_evaluation(domain, expected):
    assert matches(REC, domain) is expected


def test_policy_blocks_unknown_models_and_secrets():
    p = Policy()
    with pytest.raises(PolicyError, match="not exposed"):
        p.check_model("res.users")
    with pytest.raises(PolicyError, match="not readable"):
        p.clean_fields(["name", "password"])
    with pytest.raises(PolicyError, match="Cannot filter"):
        p.validate_domain([["api_key", "=", "x"]])
    with pytest.raises(PolicyError, match="not allowed"):
        p.validate_domain([["name", "=like", "x"]])
    with pytest.raises(PolicyError, match="expected"):
        p.validate_domain([["name", "="]])


def test_policy_write_rules_and_limits():
    with pytest.raises(PolicyError, match="read-only"):
        Policy().check_write("sale.order")
    Policy(mode="readwrite").check_write("sale.order")
    with pytest.raises(PolicyError, match="not allowed"):
        Policy(mode="readwrite").check_write("account.move")
    assert Policy(max_rows=50).clamp_limit(10_000) == 50
    assert Policy().clamp_limit(0) == 1


def test_demo_backend_read_group_by_month():
    b = DemoBackend()
    rows = b.read_group("sale.order", [["state", "=", "sale"]], ["amount_total:sum"], "date_order:month")
    assert [r["date_order:month"] for r in rows] == ["2026-08", "2026-09", "2026-10", "2026-11"]
    total = sum(o["amount_total"] for o in b.db["sale.order"] if o["state"] == "sale")
    assert sum(r["amount_total"] for r in rows) == total
