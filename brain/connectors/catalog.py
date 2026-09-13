"""Known remote MCP servers an org can add from the Connectors page.

Each entry is a public, hosted MCP endpoint. `auth` says how the org gets in:

  oauth    the server implements the MCP authorization spec (OAuth 2.1 with
           dynamic client registration). Connect = one consent screen; the brain
           keeps the tokens (brain/connectors/oauth.py). No developer-console
           app, no partnership.
  api_key  the server takes a bearer the org already holds (a PAT, an API key).
           The key is pasted once and stored in Supabase Vault.

URLs come from the vendors' published MCP endpoints (the same ones the Claude
connector directory uses). A vendor moving its endpoint shows up as a discovery
failure on Connect, with the URL editable through "Add manually".
"""

from __future__ import annotations

CATALOG: list[dict] = [
    # ── work management ──────────────────────────────────────────────────────
    {
        "id": "notion",
        "name": "Notion",
        "url": "https://mcp.notion.com/mcp",
        "category": "Work",
        "description": "Search, read and update pages and databases in a Notion workspace.",
        "auth": "oauth",
    },
    {
        "id": "linear",
        "name": "Linear",
        "url": "https://mcp.linear.app/mcp",
        "category": "Work",
        "description": "Issues, projects, cycles and comments in Linear.",
        "auth": "oauth",
    },
    {
        "id": "asana",
        "name": "Asana",
        "url": "https://mcp.asana.com/v2/mcp",
        "category": "Work",
        "description": "Tasks, projects, portfolios and goals in Asana.",
        "auth": "oauth",
    },
    {
        "id": "atlassian",
        "name": "Atlassian (Jira & Confluence)",
        "url": "https://mcp.atlassian.com/v1/mcp/authv2",
        "category": "Work",
        "description": "Jira issues and Confluence pages across the sites the account can see.",
        "auth": "oauth",
    },
    {
        "id": "monday",
        "name": "monday.com",
        "url": "https://mcp.monday.com/mcp",
        "category": "Work",
        "description": "Boards, items, workflows and dashboards in monday.com.",
        "auth": "oauth",
    },
    {
        "id": "clickup",
        "name": "ClickUp",
        "url": "https://mcp.clickup.com/mcp",
        "category": "Work",
        "description": "Tasks, comments, docs and the workspace hierarchy in ClickUp.",
        "auth": "oauth",
    },
    # ── communication & documents ────────────────────────────────────────────
    {
        "id": "slack",
        "name": "Slack",
        "url": "https://mcp.slack.com/mcp",
        "category": "Communication",
        "description": "Send messages, read channels and threads, search a Slack workspace.",
        "auth": "oauth",
    },
    {
        "id": "google_drive",
        "name": "Google Drive",
        "url": "https://drivemcp.googleapis.com/mcp/v1",
        "category": "Documents",
        "description": "Search, read and upload files in Google Drive.",
        "auth": "oauth",
    },
    {
        "id": "google_calendar",
        "name": "Google Calendar",
        "url": "https://calendarmcp.googleapis.com/mcp/v1",
        "category": "Communication",
        "description": "List, create and respond to calendar events.",
        "auth": "oauth",
    },
    {
        "id": "zoom",
        "name": "Zoom",
        "url": "https://mcp.zoom.us/mcp/zoom/streamable",
        "category": "Communication",
        "description": "Search meetings, recordings and summaries in Zoom.",
        "auth": "oauth",
    },
    {
        "id": "fireflies",
        "name": "Fireflies",
        "url": "https://api.fireflies.ai/mcp",
        "category": "Communication",
        "description": "Meeting transcripts and insights from Fireflies.",
        "auth": "oauth",
    },
    # ── customers & sales ────────────────────────────────────────────────────
    {
        "id": "hubspot",
        "name": "HubSpot",
        "url": "https://mcp.hubspot.com/anthropic",
        "category": "Customers",
        "description": "CRM objects, properties and campaign analytics in HubSpot.",
        "auth": "oauth",
    },
    {
        "id": "intercom",
        "name": "Intercom",
        "url": "https://mcp.intercom.com/mcp",
        "category": "Customers",
        "description": "Conversations and contacts in Intercom.",
        "auth": "oauth",
    },
    {
        "id": "salesforce",
        "name": "Salesforce",
        "url": "https://api.salesforce.com/platform/mcp/v1/platform/headless-360",
        "category": "Customers",
        "description": "Describe, discover and act on Salesforce records.",
        "auth": "oauth",
    },
    # ── finance ──────────────────────────────────────────────────────────────
    {
        "id": "stripe",
        "name": "Stripe",
        "url": "https://mcp.stripe.com/",
        "category": "Finance",
        "description": "Customers, payments, balances and the Stripe API surface.",
        "auth": "oauth",
    },
    {
        "id": "paypal",
        "name": "PayPal",
        "url": "https://mcp.paypal.com/mcp",
        "category": "Finance",
        "description": "Invoices, products, disputes and payments in PayPal.",
        "auth": "oauth",
    },
    {
        "id": "square",
        "name": "Square",
        "url": "https://mcp.squareup.com/mcp",
        "category": "Finance",
        "description": "Transactions, merchants and payment data in Square.",
        "auth": "oauth",
    },
    {
        "id": "quickbooks",
        "name": "QuickBooks",
        "url": "https://ai-inc.quickbooks.intuit.com/v1/mcp",
        "category": "Finance",
        "description": "Profit & loss, cash flow and transactions in QuickBooks Online.",
        "auth": "oauth",
    },
    # ── engineering ──────────────────────────────────────────────────────────
    {
        "id": "github",
        "name": "GitHub",
        "url": "https://api.githubcopilot.com/mcp/",
        "category": "Engineering",
        "description": "Repositories, issues, pull requests and code search on GitHub.",
        "auth": "api_key",
        "key_hint": "A fine-grained personal access token (github.com → Settings → Developer settings).",
    },
    {
        "id": "sentry",
        "name": "Sentry",
        "url": "https://mcp.sentry.dev/mcp",
        "category": "Engineering",
        "description": "Issues, releases and error details in Sentry.",
        "auth": "oauth",
    },
    {
        "id": "vercel",
        "name": "Vercel",
        "url": "https://mcp.vercel.com/",
        "category": "Engineering",
        "description": "Projects, deployments and logs on Vercel.",
        "auth": "oauth",
    },
    {
        "id": "cloudflare",
        "name": "Cloudflare",
        "url": "https://bindings.mcp.cloudflare.com/mcp",
        "category": "Engineering",
        "description": "Workers, KV, R2 and the rest of the Cloudflare developer platform.",
        "auth": "oauth",
    },
    # ── design & web ─────────────────────────────────────────────────────────
    {
        "id": "figma",
        "name": "Figma",
        "url": "https://mcp.figma.com/mcp",
        "category": "Design",
        "description": "Design context, screenshots and variables from Figma files.",
        "auth": "oauth",
    },
    {
        "id": "canva",
        "name": "Canva",
        "url": "https://mcp.canva.com/mcp",
        "category": "Design",
        "description": "Search, create and export Canva designs.",
        "auth": "oauth",
    },
    {
        "id": "webflow",
        "name": "Webflow",
        "url": "https://mcp.webflow.com/mcp",
        "category": "Design",
        "description": "CMS, pages, assets and sites in Webflow.",
        "auth": "oauth",
    },
    {
        "id": "shopify",
        "name": "Shopify",
        "url": "https://setup.shopify.com/mcp",
        "category": "Commerce",
        "description": "Products, orders and customers in a Shopify store.",
        "auth": "oauth",
    },
    # ── automation & data ────────────────────────────────────────────────────
    {
        "id": "zapier",
        "name": "Zapier",
        "url": "https://mcp.zapier.com/api/mcp/mcp",
        "category": "Automation",
        "description": "Run any Zapier action the account has enabled for MCP.",
        "auth": "api_key",
        "key_hint": "The server bearer from mcp.zapier.com → your MCP server → Connect.",
    },
    {
        "id": "bigquery",
        "name": "Google BigQuery",
        "url": "https://bigquery.googleapis.com/mcp",
        "category": "Data",
        "description": "Datasets, tables and SQL against BigQuery.",
        "auth": "oauth",
    },
]

_BY_ID = {e["id"]: e for e in CATALOG}


def catalog_entries() -> list[dict]:
    """The catalogue as shown in the UI (a copy — callers may annotate)."""
    return [dict(e) for e in CATALOG]


def catalog_get(catalog_id: str | None) -> dict | None:
    if not catalog_id:
        return None
    e = _BY_ID.get(str(catalog_id).strip().lower())
    return dict(e) if e else None
