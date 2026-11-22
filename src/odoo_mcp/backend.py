"""Backends that speak to Odoo.

`XmlRpcBackend` talks to a real Odoo (14-18) over the external XML-RPC API.
`DemoBackend` is an in-memory Odoo with realistic sample data, so the server,
the tests and the README demo all run without an Odoo instance.
"""

from __future__ import annotations

import operator
import xmlrpc.client
from collections import defaultdict
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, Protocol

Domain = list[Any]  # Odoo domain, e.g. [["state", "=", "sale"], ["amount_total", ">", 100]]
Record = dict[str, Any]


class OdooBackend(Protocol):
    def search_read(self, model: str, domain: Domain, fields: list[str], limit: int, order: str | None = None) -> list[Record]: ...
    def read_group(self, model: str, domain: Domain, fields: list[str], groupby: str) -> list[Record]: ...
    def create(self, model: str, values: Record) -> int: ...
    def fields(self, model: str) -> dict[str, str]: ...


class XmlRpcBackend:
    """Thin wrapper over Odoo's external API. Authenticates with an API key, never a password."""

    def __init__(self, url: str, db: str, user: str, api_key: str):
        self.url, self.db, self.api_key = url.rstrip("/"), db, api_key
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)
        uid = common.authenticate(db, user, api_key, {})
        if not uid:
            raise PermissionError("Odoo rejected the credentials (check ODOO_USER / ODOO_API_KEY)")
        self.uid = int(uid)  # type: ignore[arg-type]
        self._obj = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)

    def _call(self, model: str, method: str, args: list[Any], kwargs: dict[str, Any] | None = None) -> Any:
        return self._obj.execute_kw(self.db, self.uid, self.api_key, model, method, args, kwargs or {})

    def search_read(self, model, domain, fields, limit, order=None):
        kw: dict[str, Any] = {"fields": fields, "limit": limit}
        if order:
            kw["order"] = order
        return self._call(model, "search_read", [domain], kw)

    def read_group(self, model, domain, fields, groupby):
        return self._call(model, "read_group", [domain, fields, [groupby]], {"lazy": True})

    def create(self, model, values):
        return int(self._call(model, "create", [values]))

    def fields(self, model):
        meta = self._call(model, "fields_get", [], {"attributes": ["type"]})
        return {name: info["type"] for name, info in meta.items()}


# ---------------------------------------------------------------- demo backend

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "=": operator.eq, "!=": operator.ne, ">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le,
    "in": lambda a, b: a in b, "not in": lambda a, b: a not in b,
    "ilike": lambda a, b: str(b).lower() in str(a or "").lower(),
    "not ilike": lambda a, b: str(b).lower() not in str(a or "").lower(),
}


def _field_value(rec: Record, field: str) -> Any:
    v = rec.get(field)
    return v[0] if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int) else v  # many2one -> id


def matches(rec: Record, domain: Domain) -> bool:
    """Evaluate an Odoo domain (prefix notation with '&', '|', '!'; implicit AND)."""
    stack: list[bool] = []
    for term in reversed(domain):
        if term == "!":
            stack.append(not stack.pop())
        elif term in ("&", "|"):
            a, b = stack.pop(), stack.pop()
            stack.append((a and b) if term == "&" else (a or b))
        else:
            field, op, value = term
            if op not in _OPS:
                raise ValueError(f"Unsupported domain operator: {op!r}")
            try:
                stack.append(bool(_OPS[op](_field_value(rec, field), value)))
            except TypeError:  # e.g. comparing None with a number
                stack.append(False)
    return all(stack)


class DemoBackend:
    """An in-memory Odoo with a small coffee roaster's data (fictional)."""

    def __init__(self) -> None:
        self.db: dict[str, list[Record]] = _demo_data()

    def search_read(self, model, domain, fields, limit, order=None):
        rows = [r for r in self.db.get(model, []) if matches(r, domain)]
        if order:
            for part in reversed(order.split(",")):
                key, *direction = part.strip().split()
                desc = bool(direction and direction[0].lower() == "desc")
                rows.sort(key=lambda r: (r.get(key) is None, r.get(key)), reverse=desc)
        return [{f: r.get(f) for f in ["id", *fields]} for r in rows[:limit]]

    def read_group(self, model, domain, fields, groupby):
        key, _, interval = groupby.partition(":")
        sums = [f.split(":")[0] for f in fields if f.split(":")[0] != key]
        groups: dict[str, Record] = defaultdict(dict)
        for r in (r for r in self.db.get(model, []) if matches(r, domain)):
            g = r.get(key)
            if interval == "month" and g:
                g = str(g)[:7]
            row = groups[str(g)]
            row.setdefault(groupby, g)
            row[f"{key}_count"] = row.get(f"{key}_count", 0) + 1
            for f in sums:
                row[f] = round(row.get(f, 0) + (r.get(f) or 0), 2)
        return sorted(groups.values(), key=lambda r: str(r[groupby]))

    def create(self, model, values):
        rec = {"id": max((r["id"] for r in self.db.get(model, [])), default=0) + 1, **values}
        if model == "sale.order":  # Odoo assigns the sequence number on create
            rec.setdefault("name", f"S{rec['id']:05d}")
        self.db.setdefault(model, []).append(rec)
        return rec["id"]

    def fields(self, model):
        sample = (self.db.get(model) or [{}])[0]
        return {k: type(v).__name__ for k, v in sample.items()}


def _demo_data() -> dict[str, list[Record]]:
    partners = [
        (1, "Kopi Senja Cafe", "Jakarta"), (2, "Lantai Dua Coffee", "Bandung"), (3, "Hotel Arunika", "Yogyakarta"),
        (4, "Ruang Temu Co-working", "Jakarta"), (5, "Bakery Rumah Roti", "Surabaya"), (6, "PT Sinar Retail", "Jakarta"),
    ]
    products = [  # id, name, sku, price (IDR), on hand
        (1, "Gayo Arabica 250g", "BEAN-GAYO-250", 145000, 84), (2, "Toraja Kalosi 250g", "BEAN-TRJ-250", 165000, 12),
        (3, "Kintamani Natural 1kg", "BEAN-KIN-1K", 520000, 6), (4, "House Blend 1kg", "BEAN-HB-1K", 310000, 140),
        (5, "Cold Brew Bottle 250ml", "CB-250", 32000, 0), (6, "V60 Paper Filter x100", "ACC-V60F", 85000, 55),
    ]
    plan = [  # partner, order date, state, [(product, qty)]
        (1, "2026-08-04", "sale", [(4, 10), (6, 4)]), (2, "2026-08-11", "sale", [(1, 24), (2, 12)]),
        (3, "2026-08-25", "sale", [(4, 30)]), (6, "2026-09-02", "sale", [(1, 60), (5, 120)]),
        (1, "2026-09-15", "sale", [(4, 12), (3, 2)]), (4, "2026-09-28", "cancel", [(5, 48)]),
        (5, "2026-10-03", "sale", [(4, 8), (6, 6)]), (2, "2026-10-17", "sale", [(2, 18), (3, 4)]),
        (3, "2026-10-30", "sale", [(4, 36), (1, 20)]), (6, "2026-11-06", "sale", [(1, 80), (2, 40)]),
        (1, "2026-11-12", "draft", [(4, 14)]), (4, "2026-11-18", "draft", [(5, 96), (6, 10)]),
    ]
    pname = {p[0]: p[1] for p in products}
    price = {p[0]: p[3] for p in products}
    cname = {p[0]: p[1] for p in partners}
    orders: list[Record] = []
    lines: list[Record] = []
    moves: list[Record] = []
    for oid, (pid, day, state, items) in enumerate(plan, start=1):
        total = 0
        for prod, qty in items:
            sub = price[prod] * qty
            total += sub
            lines.append({"id": len(lines) + 1, "order_id": [oid, f"S{oid:05d}"], "product_id": [prod, pname[prod]],
                          "product_uom_qty": qty, "price_unit": price[prod], "price_subtotal": sub})
        orders.append({"id": oid, "name": f"S{oid:05d}", "partner_id": [pid, cname[pid]], "date_order": day,
                       "state": state, "amount_total": total})
        if state == "sale":
            paid = oid in (1, 2, 3, 5, 7)
            moves.append({"id": len(moves) + 1, "name": f"INV/2026/{oid:04d}", "move_type": "out_invoice",
                          "partner_id": [pid, cname[pid]], "invoice_origin": f"S{oid:05d}", "invoice_date": day,
                          "invoice_date_due": (date.fromisoformat(day) + timedelta(days=30)).isoformat(),
                          "amount_total": total, "amount_residual": 0 if paid else total,
                          "payment_state": "paid" if paid else "not_paid", "state": "posted"})
    return {
        "res.partner": [{"id": i, "name": n, "city": c, "email": f"orders@{n.split()[0].lower()}.example",
                         "is_company": True, "customer_rank": 1} for i, n, c in partners],
        "product.product": [{"id": i, "name": n, "default_code": sku, "list_price": p, "qty_available": q, "type": "product"}
                            for i, n, sku, p, q in products],
        "sale.order": orders,
        "sale.order.line": lines,
        "account.move": moves,
        "res.users": [{"id": 2, "name": "Administrator", "login": "admin", "password": "not-a-real-hash"}],
    }
