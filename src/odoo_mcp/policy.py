"""Guardrails between the model and the ERP. Pure functions, so every rule is unit-tested."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

Mode = Literal["readonly", "readwrite"]

DEFAULT_MODELS = frozenset({
    "res.partner", "product.product", "product.template", "sale.order", "sale.order.line",
    "account.move", "stock.quant", "purchase.order", "crm.lead", "project.task",
})
# never exposed, whatever the model: credentials, tokens, binary blobs
SENSITIVE_FIELDS = frozenset({
    "password", "api_key", "api_key_ids", "totp_secret", "signup_token", "oauth_access_token", "image_1920", "datas",
})
_ALLOWED_OPS = {"=", "!=", ">", ">=", "<", "<=", "in", "not in", "ilike", "not ilike"}


class PolicyError(ToolError):
    """A request the policy refuses. The message is safe to show to the model so it can self-correct."""


@dataclass(frozen=True)
class Policy:
    mode: Mode = "readonly"
    models: frozenset[str] = DEFAULT_MODELS
    writable_models: frozenset[str] = frozenset({"sale.order"})
    max_rows: int = 200

    def check_model(self, model: str) -> None:
        if model not in self.models:
            raise PolicyError(f"Model '{model}' is not exposed. Allowed: {', '.join(sorted(self.models))}")

    def check_write(self, model: str) -> None:
        if self.mode != "readwrite":
            raise PolicyError("This server is read-only (start it with ODOO_MCP_MODE=readwrite to enable write tools)")
        if model not in self.writable_models:
            raise PolicyError(f"Writes to '{model}' are not allowed")

    def clean_fields(self, fields: list[str]) -> list[str]:
        bad = [f for f in fields if f in SENSITIVE_FIELDS or f.startswith("__")]
        if bad:
            raise PolicyError(f"Field(s) not readable: {', '.join(bad)}")
        return fields

    def clamp_limit(self, limit: int) -> int:
        return max(1, min(limit, self.max_rows))

    def validate_domain(self, domain: Any) -> list[Any]:
        """Accept only well-formed domains, so a confused model can't send arbitrary payloads."""
        if not isinstance(domain, list):
            raise PolicyError("domain must be a list of [field, operator, value] terms")
        out: list[Any] = []
        for term in domain:
            if term in ("&", "|", "!"):
                out.append(term)
                continue
            if not (isinstance(term, (list, tuple)) and len(term) == 3 and isinstance(term[0], str)):
                raise PolicyError(f"Bad domain term {term!r}: expected [field, operator, value]")
            if term[1] not in _ALLOWED_OPS:
                raise PolicyError(f"Operator {term[1]!r} is not allowed")
            if term[0].split(".")[0] in SENSITIVE_FIELDS:
                raise PolicyError(f"Cannot filter on '{term[0]}'")
            out.append(list(term))
        return out
