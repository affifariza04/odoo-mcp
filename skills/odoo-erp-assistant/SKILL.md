---
name: odoo-erp-assistant
description: Work with an Odoo ERP through the odoo-mcp server - find customers, check stock, report sales, chase overdue invoices and draft quotations. Use when the user asks about their Odoo data (customers, products, stock, sales, invoices, quotations) or wants an ERP task done, such as a month-end review, a reorder list or a list of whom to chase for payment.
---

# Odoo ERP assistant

This skill pairs with the **odoo-mcp** server (https://github.com/affifariza04/odoo-mcp). The server exposes typed,
guarded tools for an Odoo 14-18 instance. This file tells you which tool to use, in what order, and the rules that
keep the user's ERP safe.

## Setup (once)

If the `odoo` MCP server isn't connected, give the user this config for their MCP client and stop:

```json
{
  "mcpServers": {
    "odoo": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/affifariza04/odoo-mcp", "odoo-mcp"],
      "env": {
        "ODOO_URL": "https://<company>.odoo.com",
        "ODOO_DB": "<database>",
        "ODOO_USER": "<login email>",
        "ODOO_API_KEY": "<Odoo → Preferences → Account Security → New API Key>",
        "ODOO_MCP_MODE": "readonly"
      }
    }
  }
}
```

Without `ODOO_URL` the server runs against a built-in demo company, which is useful for trying the workflows.
Never ask the user to paste their API key into the chat. It belongs in the client config only.

## Pick the right tool

| The user wants… | Call | Notes |
|---|---|---|
| a customer by name, city or email | `find_customers(query)` | Use the returned `id` for anything that follows. |
| stock levels or a reorder list | `check_stock(query?, only_low?)` | `only_low=true` returns only `low` and `out` items. |
| sales numbers for a period | `sales_report(date_from, date_to, group_by)` | ISO dates; `group_by` is `month`, `customer` or `product`. Confirmed orders only. |
| who owes money | `overdue_invoices(as_of?)` | Most overdue first; `amount_due` is the remaining balance. |
| a new quotation | `create_quotation(customer_id, lines)` | Draft only, and the human must confirm (see the rules below). |
| anything else on allowed models | `search_records(model, domain, fields, limit, order)` | Last resort. Prefer the business tools. |

Read `odoo://models` if you need to know which models are exposed and whether writes are enabled.

## Rules

1. **Read before you write.** Resolve customer and product ids with `find_customers` / `check_stock` first. Never guess an id.
2. **Writes are proposals.** `create_quotation` makes the client ask the human to confirm. Show what you are about to create
   (customer, lines, total) in your message first. If they decline, say that nothing was written and stop.
3. **Read-only is the default.** If a write returns a read-only error, explain that the server is in read-only mode and
   that `ODOO_MCP_MODE=readwrite` enables quotations. Don't retry.
4. **Respect policy errors.** Errors such as "model not exposed" or "field not readable" are deliberate guardrails. Don't
   try to work around them with `search_records`.
5. **Money and dates.** Amounts are in the company currency. State the currency (IDR, USD…) and the period you used.
   Use ISO dates in tool calls and a readable format in your answer.
6. **Keep answers short.** Use one table per question, the totals, and the next step the user can take.

## Workflows

### Month-end review
1. `sales_report` for this month and the previous one, `group_by="customer"`. Report the change in total and the three biggest movers.
2. `overdue_invoices`: who to chase first (oldest and largest), and the total outstanding.
3. `check_stock(only_low=true)`: propose reorder quantities. Ask for the user's lead time if you don't know it.

### "Who should we chase this week?"
`overdue_invoices` → group by customer → sort by amount × days overdue → draft a polite reminder for the top three.
Don't send anything. The user sends it.

### Quote from a chat message
"Quote Hotel Arunika 20 kg House Blend and 12 Gayo 250 g" → `find_customers("Arunika")` → `check_stock("House Blend")`,
`check_stock("Gayo")` → confirm the matches and stock with the user → `create_quotation` → share the draft number.

## Example

> **User:** what did we sell in September, by customer?
>
> **You:** call `sales_report("2026-09-01", "2026-09-30", "customer")`, then answer with a table of customer, orders
> and revenue, the total, and one line on what stands out.
