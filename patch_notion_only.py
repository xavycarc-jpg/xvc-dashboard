#!/usr/bin/env python3
"""
patch_notion_only.py
Fetch Notion pipeline data and bake into index.html, then push to GitHub.
Run with: NOTION_TOKEN=xxx GITHUB_PAT=xxx python3 patch_notion_only.py
"""

import os, re, subprocess
import requests
from pathlib import Path

NOTION_TOKEN          = os.environ["NOTION_TOKEN"]
NOTION_PIPELINE_DB_ID = "546900d5-7dbf-4c20-83d1-4953d151dde1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_FILE = os.path.join(SCRIPT_DIR, "index.html")
GH_TOKEN       = os.environ.get("GITHUB_PAT", "")


# ── helpers ───────────────────────────────────────────────────────────────────

def prop_val(prop):
    if not prop:
        return ""
    t = prop.get("type", "")
    if t == "title":
        return "".join(r["plain_text"] for r in prop.get("title", []))
    if t == "rich_text":
        return "".join(r["plain_text"] for r in prop.get("rich_text", []))
    if t == "select":
        s = prop.get("select")
        return s["name"] if s else ""
    if t == "multi_select":
        return ", ".join(o["name"] for o in prop.get("multi_select", []))
    if t == "date":
        d = prop.get("date")
        return d["start"] if d else ""
    if t == "url":
        return prop.get("url") or ""
    if t == "number":
        n = prop.get("number")
        return str(n) if n is not None else ""
    if t == "formula":
        f = prop.get("formula", {})
        ft = f.get("type", "")
        if ft == "string":
            return f.get("string") or ""
        if ft == "number":
            n = f.get("number")
            return str(n) if n is not None else ""
    if t == "rollup":
        r = prop.get("rollup", {})
        if r.get("type") == "number":
            n = r.get("number")
            return str(n) if n is not None else ""
    return ""


def stag(status):
    published   = {"Published", "Ready to Publish", "Scheduled Post"}
    in_progress = {
        "Top Projects Chosen", "Research + Brainstorm", "Outline",
        "Script Development", "Script: Visual Map", "Ready to Film",
        "Filming: Production", "VO: Production", "VO: Revision",
        "Organizing Files > Edit", "Hook Only", "C1: Story", "C2: Audio",
        "C3: Color", "C4: Polish",
    }
    if not status:
        return '<span class="tag tn">—</span>'
    cls = "tg" if status in published else "tb" if status in in_progress else "tn"
    return f'<span class="tag {cls}">{status}</span>'


def query_notion(flt, sorts=None):
    pages, has_more, cursor = [], True, None
    payload = {"page_size": 100, "filter": flt}
    if sorts:
        payload["sorts"] = sorts
    while has_more:
        if cursor:
            payload["start_cursor"] = cursor
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_PIPELINE_DB_ID}/query",
            headers=NOTION_HEADERS, json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        pages.extend(data.get("results", []))
        has_more = data.get("has_more", False)
        cursor   = data.get("next_cursor")
    return pages


# ── fetchers ──────────────────────────────────────────────────────────────────

def fetch_pipeline():
    statuses = [
        "Top Projects Chosen", "Research + Brainstorm", "Outline",
        "Script Development", "Script: Visual Map", "Ready to Film",
        "Filming: Production", "VO: Production", "VO: Revision",
        "Organizing Files > Edit",
    ]
    return query_notion({"or": [{"property": "PL Status", "select": {"equals": s}} for s in statuses]})


def fetch_editor():
    phases = ["Hook Only", "C1: Story", "C2: Audio", "C3: Color", "C4: Polish"]
    return query_notion(
        {"or": [{"property": "Edit Phase", "select": {"equals": p}} for p in phases]},
        [{"property": "Next Cut Due", "direction": "ascending"}],
    )


def fetch_publishing():
    statuses = ["Published", "Ready to Publish", "Scheduled Post"]
    return query_notion(
        {"and": [
            {"or": [{"property": "PL Status", "select": {"equals": s}} for s in statuses]},
            {"property": "Format", "multi_select": {"contains": "Long form"}},
        ]},
        [{"property": "Publish Date", "direction": "descending"}],
    )


def fetch_billing():
    invoices = [
        "A. Full Prod Invoice", "C. Invoice: 2025",
        "D. Invoice: 2026 (March)", "E. Unassigned",
    ]
    return query_notion({"or": [{"property": "Invoice Credit", "select": {"equals": i}} for i in invoices]})


# ── row builders ──────────────────────────────────────────────────────────────

def rows_pipeline(pages):
    out = []
    for p in pages:
        pr = p["properties"]
        title     = prop_val(pr.get("VLOG Official Title", {}))
        pl_status = prop_val(pr.get("PL Status", {}))
        script_st = prop_val(pr.get("Script Status", {}))
        filming   = prop_val(pr.get("Filming Start", {}))
        pub_date  = prop_val(pr.get("Publish Date", {}))
        out.append(
            f'<tr>'
            f'<td style="font-weight:500;max-width:220px">{title or "—"}</td>'
            f'<td>{stag(pl_status)}</td>'
            f'<td><span class="mono" style="font-size:11px">{script_st or "—"}</span></td>'
            f'<td class="mono" style="color:rgba(240,239,232,0.4);font-size:11px">{filming or "—"}</td>'
            f'<td class="mono" style="color:rgba(240,239,232,0.4);font-size:11px">{pub_date or "—"}</td>'
            f'</tr>'
        )
    return "\n".join(out)


def rows_editor(pages):
    out = []
    for p in pages:
        pr = p["properties"]
        title    = prop_val(pr.get("VLOG Official Title", {}))
        phase    = prop_val(pr.get("Edit Phase", {}))
        progress = prop_val(pr.get("Edit: Progress", {}))
        cut_due  = prop_val(pr.get("Next Cut Due", {}))
        pub_date = prop_val(pr.get("Publish Date", {}))
        due_col  = "var(--amber)" if cut_due else "rgba(240,239,232,0.4)"
        out.append(
            f'<tr>'
            f'<td style="font-weight:500;max-width:220px">{title or "—"}</td>'
            f'<td>{stag(phase)}</td>'
            f'<td><span class="mono" style="font-size:11px">{progress or "—"}</span></td>'
            f'<td class="mono" style="color:{due_col};font-size:11px">{cut_due or "—"}</td>'
            f'<td class="mono" style="color:rgba(240,239,232,0.4);font-size:11px">{pub_date or "—"}</td>'
            f'</tr>'
        )
    return "\n".join(out)


def rows_publishing(pages):
    out = []
    for p in pages:
        pr = p["properties"]
        title    = prop_val(pr.get("VLOG Official Title", {}))
        pub_date = prop_val(pr.get("Publish Date", {}))
        yt_link  = prop_val(pr.get("YT Link", {}))
        views    = prop_val(pr.get("Views", {}))
        pl_st    = prop_val(pr.get("PL Status", {}))
        season   = prop_val(pr.get("Season & Show", {}))
        yt_cell  = (
            f'<a href="{yt_link}" target="_blank" style="color:var(--teal);font-size:11px">↗ YouTube</a>'
            if yt_link else '<span style="color:rgba(240,239,232,0.25)">—</span>'
        )
        try:
            views_fmt = f'{int(float(views)):,}' if views else "—"
        except (ValueError, TypeError):
            views_fmt = views or "—"
        out.append(
            f'<tr>'
            f'<td style="font-weight:500;max-width:200px">{title or "—"}</td>'
            f'<td class="mono" style="color:rgba(240,239,232,0.4);font-size:11px">{pub_date or "—"}</td>'
            f'<td>{yt_cell}</td>'
            f'<td class="mono" style="color:var(--teal);font-weight:600">{views_fmt}</td>'
            f'<td>{stag(pl_st)}</td>'
            f'<td style="font-size:11px;color:rgba(240,239,232,0.4)">{season or "—"}</td>'
            f'</tr>'
        )
    return "\n".join(out)


def rows_billing(pages):
    out = []
    for p in pages:
        pr = p["properties"]
        title    = prop_val(pr.get("VLOG Official Title", {}))
        invoice  = prop_val(pr.get("Invoice Credit", {}))
        payment  = prop_val(pr.get("Edits Payment", {}))
        progress = prop_val(pr.get("Edit: Progress", {}))
        credits  = prop_val(pr.get("Credit Count", {}))
        season   = prop_val(pr.get("Season & Show", {}))
        try:
            pay_fmt = f'${float(payment):,.2f}' if payment else "—"
        except (ValueError, TypeError):
            pay_fmt = payment or "—"
        out.append(
            f'<tr>'
            f'<td style="font-weight:500;max-width:200px">{title or "—"}</td>'
            f'<td style="font-size:11px">{invoice or "—"}</td>'
            f'<td class="mono" style="color:var(--teal);font-weight:600">{pay_fmt}</td>'
            f'<td><span class="mono" style="font-size:11px">{progress or "—"}</span></td>'
            f'<td class="mono" style="color:rgba(240,239,232,0.4)">{credits or "—"}</td>'
            f'<td style="font-size:11px;color:rgba(240,239,232,0.4)">{season or "—"}</td>'
            f'</tr>'
        )
    return "\n".join(out)


# ── patch + push ──────────────────────────────────────────────────────────────

def replace_tbody(html, tbody_id, rows):
    return re.sub(
        rf'(<tbody id="{tbody_id}">).*?(</tbody>)',
        lambda m: m.group(1) + rows + m.group(2),
        html, flags=re.DOTALL,
    )


def replace_count(html, elem_id, text):
    return re.sub(
        rf'(<span[^>]+id="{elem_id}"[^>]*>)[^<]*(</span>)',
        rf'\g<1>{text}\g<2>',
        html,
    )


def main():
    print("── Notion-only patch ──────────────────────────────────")

    print("Fetching Pipeline tab …")
    pipeline_p = fetch_pipeline()
    print(f"  {len(pipeline_p)} rows")

    print("Fetching Editor tab …")
    editor_p = fetch_editor()
    print(f"  {len(editor_p)} rows")

    print("Fetching Publishing tab …")
    pub_p = fetch_publishing()
    print(f"  {len(pub_p)} rows")

    print("Fetching Billing tab …")
    billing_p = fetch_billing()
    print(f"  {len(billing_p)} rows")

    html = Path(DASHBOARD_FILE).read_text(encoding="utf-8")

    html = replace_tbody(html, "pipeline-tbody",   rows_pipeline(pipeline_p))
    html = replace_tbody(html, "editor-tbody",     rows_editor(editor_p))
    html = replace_tbody(html, "publishing-tbody", rows_publishing(pub_p))
    html = replace_tbody(html, "billing-tbody",    rows_billing(billing_p))

    html = replace_count(html, "pipeline-count",   f"{len(pipeline_p)} projects")
    html = replace_count(html, "editor-count",     f"{len(editor_p)} in edit")
    html = replace_count(html, "publishing-count", f"{len(pub_p)} published")
    html = replace_count(html, "billing-count",    f"{len(billing_p)} invoices")

    Path(DASHBOARD_FILE).write_text(html, encoding="utf-8")
    print("index.html patched")

    if GH_TOKEN:
        remote = f"https://{GH_TOKEN}@github.com/xavycarc-jpg/xvc-dashboard.git"
        subprocess.run(["git", "remote", "set-url", "origin", remote],
                       cwd=SCRIPT_DIR, check=True, capture_output=True)
    subprocess.run(["git", "add", "index.html"], cwd=SCRIPT_DIR, check=True)
    r = subprocess.run(["git", "commit", "-m", "chore: bake Notion tab data into HTML"],
                       cwd=SCRIPT_DIR, capture_output=True, text=True)
    if "nothing to commit" in r.stdout + r.stderr:
        print("nothing to commit")
        return
    subprocess.run(["git", "push", "origin", "main"], cwd=SCRIPT_DIR, check=True)
    print("pushed ✓")
    print("Live at https://xavycarc-jpg.github.io/xvc-dashboard/")


if __name__ == "__main__":
    main()
