"""Known remote MCP servers an org can add from the Connectors page.

Every entry is a direct connection from this deployment to the vendor's own
hosted MCP server. The list was seeded from the vendors' published endpoints
(the same ones Claude's connector directory points at), but nothing is borrowed
from Claude: an org's sign-in in claude.ai does not carry over, and the tokens
live here. `auth` says how the org gets in:

  oauth    the server implements the MCP authorization spec (OAuth 2.1).
           Connect = one consent screen; the brain keeps the tokens
           (brain/connectors/oauth.py).
  api_key  the server takes a bearer the org already holds (a PAT, an API key).
           The key is pasted once and stored in Supabase Vault.

`app` marks a vendor whose authorization server only accepts pre-registered
clients (no RFC 7591 self-registration): Google, Slack, Asana, Zoom, HubSpot.
Claude connects to those because Anthropic registered an app with each one.
Here the deployment can hold its own app for the vendor
(BRAIN_OAUTH_<APP>_CLIENT_ID / _CLIENT_SECRET, see platform_app), which makes
Connect one click again; without it the org pastes a client id + secret from
the vendor's console. brain/connectors/setup_check.py re-derives all of this
from the live servers, so the page shows what Connect will actually do.
`scope` narrows what a server advertises (Google Calendar lists twelve scopes)
to what the agent needs.

Refreshing the list: run scripts/check_connector_catalog.py to see what every
entry does today. New entries come from Claude's connector directory (Claude
Code's MCP registry search returns each server's URL) — add them here.
"""

from __future__ import annotations

import os

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
        "app": "asana",
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
        "app": "slack",
    },
    {
        "id": "google_drive",
        "name": "Google Drive",
        "url": "https://drivemcp.googleapis.com/mcp/v1",
        "category": "Documents",
        "description": "Search, read and upload files in Google Drive.",
        "auth": "oauth",
        "app": "google",
        "scope": "https://www.googleapis.com/auth/drive.readonly "
        "https://www.googleapis.com/auth/drive.file",
    },
    {
        "id": "google_calendar",
        "name": "Google Calendar",
        "url": "https://calendarmcp.googleapis.com/mcp/v1",
        "category": "Communication",
        "description": "List, create and respond to calendar events.",
        "auth": "oauth",
        "app": "google",
        "scope": "https://www.googleapis.com/auth/calendar.events "
        "https://www.googleapis.com/auth/calendar.readonly",
    },
    {
        "id": "zoom",
        "name": "Zoom",
        "url": "https://mcp.zoom.us/mcp/zoom/streamable",
        "category": "Communication",
        "description": "Search meetings, recordings and summaries in Zoom.",
        "auth": "oauth",
        "app": "zoom",
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
        "app": "hubspot",
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
        "app": "google",
    },
]

# Vendors that only accept pre-registered OAuth clients. `console` is where an
# org (or the operator, for the platform app) creates one.
APPS: dict[str, dict] = {
    "google": {"name": "Google", "console": "https://console.cloud.google.com/apis/credentials"},
    "slack": {"name": "Slack", "console": "https://api.slack.com/apps"},
    "asana": {"name": "Asana", "console": "https://app.asana.com/0/my-apps"},
    "zoom": {"name": "Zoom", "console": "https://marketplace.zoom.us/develop/create"},
    "hubspot": {"name": "HubSpot", "console": "https://developers.hubspot.com/"},
}

_BY_ID = {e["id"]: e for e in CATALOG}


def catalog_entries() -> list[dict]:
    """The catalogue as shown in the UI (a copy — callers may annotate)."""
    return [dict(e) for e in CATALOG]


def catalog_get(catalog_id: str | None) -> dict | None:
    if not catalog_id:
        return None
    e = _BY_ID.get(str(catalog_id).strip().lower())
    return dict(e) if e else None


def platform_app(app: str | None) -> dict | None:
    """The deployment's own OAuth client for a vendor, or None.

    BRAIN_OAUTH_<APP>_CLIENT_ID / BRAIN_OAUTH_<APP>_CLIENT_SECRET. Both are
    required: every vendor in APPS is a confidential-client-only provider."""
    if not app:
        return None
    key = str(app).strip().upper()
    cid = os.environ.get(f"BRAIN_OAUTH_{key}_CLIENT_ID", "").strip()
    sec = os.environ.get(f"BRAIN_OAUTH_{key}_CLIENT_SECRET", "").strip()
    if not cid or not sec:
        return None
    return {"client_id": cid, "client_secret": sec}
