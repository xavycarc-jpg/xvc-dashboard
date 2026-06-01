"""
update_dashboard.py
Pull live YouTube + Notion data, patch index.html, push to GitHub.
"""

import os, re, subprocess
import json
from pathlib import Path

STATE_FILE = Path(__file__).parent / 'state.json'
MILESTONES = [1000, 5000, 10000, 25000, 50000, 100000]

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(video_stats):
    state = {v['id']: v['views'] for v in video_stats}
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def check_milestones(video_stats):
    state = load_state()
    hits = []
    for v in video_stats:
        prev = state.get(v['id'], 0)
        curr = v['views']
        for m in MILESTONES:
            if prev < m <= curr:
                hits.append((v['title'], m))
    return hits

def check_new_publishes(video_stats):
    state = load_state()
    return [v for v in video_stats if v['id'] not in state]

from datetime import date

import requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────────
SCOPES = [
    'https://www.googleapis.com/auth/youtube.readonly',
    'https://www.googleapis.com/auth/yt-analytics.readonly',
]
SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(SCRIPT_DIR, 'credentials.json')
TOKEN_FILE       = os.path.join(SCRIPT_DIR, 'token.json')

NOTION_TOKEN = os.environ.get('NOTION_TOKEN')
NOTION_DB_ID   = 'dd646e89-94f2-43a3-8810-fd69f5fa8486'
NOTION_HEADERS = {
    'Authorization': f'Bearer {NOTION_TOKEN}',
    'Content-Type': 'application/json',
    'Notion-Version': '2022-06-28',
}

DASHBOARD_FILE = 'index.html'
DASHBOARD_DIR  = '.'
GH_TOKEN = os.environ.get('GITHUB_PAT')

fmt  = lambda n: f'{int(n):,}'
pct  = lambda p: f'{p:.1f}%'
mmss = lambda s: f'{int(s)//60:02d}:{int(s)%60:02d}'


# ── Auth ──────────────────────────────────────────────────────────────────────
def get_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
    return creds


# ── YouTube ───────────────────────────────────────────────────────────────────
def get_channel_info(youtube):
    resp = youtube.channels().list(part='statistics,contentDetails', mine=True).execute()
    item = resp['items'][0]
    return {
        'subs':             int(item['statistics']['subscriberCount']),
        'uploads_playlist': item['contentDetails']['relatedPlaylists']['uploads'],
    }


def get_all_video_ids(youtube, playlist_id):
    ids, page_token = [], None
    while True:
        resp = youtube.playlistItems().list(
            part='contentDetails', playlistId=playlist_id,
            maxResults=50, pageToken=page_token,
        ).execute()
        ids.extend(i['contentDetails']['videoId'] for i in resp['items'])
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return ids


def classify_videos(youtube, video_ids):
    """Split into (lf_ids, sf_ids) by duration — <180 s = short form."""
    lf, sf = [], []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = youtube.videos().list(part='contentDetails', id=','.join(batch)).execute()
        for item in resp['items']:
            m = re.search(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?',
                          item['contentDetails']['duration'])
            total = (int(m.group(1) or 0) * 3600
                     + int(m.group(2) or 0) * 60
                     + int(m.group(3) or 0))
            (sf if total < 180 else lf).append(item['id'])
    return lf, sf


def get_analytics(yta, video_ids):
    today    = date.today().isoformat()
    analytics = {}
    for i in range(0, len(video_ids), 50):
        batch   = video_ids[i:i + 50]
        filters = 'video==' + ','.join(batch)
        try:
            resp = yta.reports().query(
                ids='channel==MINE',
                startDate='2005-01-01',
                endDate=today,
                dimensions='video',
                metrics='views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained',
                filters=filters,
            ).execute()
            for row in resp.get('rows', []):
                vid, views, emw, avd, avp, subs = row
                analytics[vid] = {
                    'views':         int(views),
                    'watch_minutes': float(emw),
                    'avg_dur_secs':  float(avd),
                    'avg_pct':       float(avp),
                    'subs':          int(subs),
                }
        except Exception as e:
            print(f'  Analytics warning (batch {i//50+1}): {e}')
            # Fallback: retry without estimatedMinutesWatched
            try:
                resp = yta.reports().query(
                    ids='channel==MINE',
                    startDate='2005-01-01',
                    endDate=today,
                    dimensions='video',
                    metrics='views,averageViewDuration,averageViewPercentage,subscribersGained',
                    filters=filters,
                ).execute()
                for row in resp.get('rows', []):
                    vid, views, avd, avp, subs = row
                    analytics[vid] = {
                        'views':         int(views),
                        'watch_minutes': int(views) * float(avd) / 60,  # estimate
                        'avg_dur_secs':  float(avd),
                        'avg_pct':       float(avp),
                        'subs':          int(subs),
                    }
            except Exception as e2:
                print(f'  Fallback also failed: {e2}')
    return analytics


# ── Notion ────────────────────────────────────────────────────────────────────

def get_video_titles(youtube, video_ids):
    titles = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = youtube.videos().list(part='snippet', id=','.join(batch)).execute()
        for item in resp['items']:
            titles[item['id']] = item['snippet']['title']
    return titles

def get_s1_video_ids():
    """Return YouTube video IDs for all S1 records that have a YT Link."""
    ids, has_more, cursor = [], True, None
    while has_more:
        payload = {'page_size': 100}
        if cursor:
            payload['start_cursor'] = cursor
        resp = requests.post(
            f'https://api.notion.com/v1/databases/{NOTION_DB_ID}/query',
            headers=NOTION_HEADERS, json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        for page in data.get('results', []):
            props  = page['properties']
            season = ((props.get('Season & Show') or {}).get('select') or {}).get('name', '')
            if not season.upper().startswith('S1'):
                continue
            yt_url = ((props.get('YT Link') or {}).get('url')) or ''
            m = re.search(r'[?&]v=([A-Za-z0-9_-]{11})', yt_url) or \
                re.search(r'youtu\.be/([A-Za-z0-9_-]{11})', yt_url)
            if m:
                ids.append(m.group(1))
        has_more = data.get('has_more', False)
        cursor   = data.get('next_cursor')
    return ids



NOTION_PIPELINE_DB_ID = '37e407332f264183acdde5631333f803'


def prop_val(prop):
    """Extract plain text from any Notion property type."""
    if not prop:
        return ''
    t = prop.get('type', '')
    if t == 'title':
        return ''.join(r['plain_text'] for r in prop.get('title', []))
    if t == 'rich_text':
        return ''.join(r['plain_text'] for r in prop.get('rich_text', []))
    if t == 'select':
        s = prop.get('select')
        return s['name'] if s else ''
    if t == 'multi_select':
        return ', '.join(o['name'] for o in prop.get('multi_select', []))
    if t == 'date':
        d = prop.get('date')
        return d['start'] if d else ''
    if t == 'url':
        return prop.get('url') or ''
    if t == 'number':
        n = prop.get('number')
        return str(n) if n is not None else ''
    if t == 'formula':
        f = prop.get('formula', {})
        ft = f.get('type', '')
        if ft == 'string':
            return f.get('string') or ''
        if ft == 'number':
            n = f.get('number')
            return str(n) if n is not None else ''
    if t == 'rollup':
        r = prop.get('rollup', {})
        if r.get('type') == 'number':
            n = r.get('number')
            return str(n) if n is not None else ''
    return ''


def status_cls(status):
    published  = {'Published', 'Ready to Publish', 'Scheduled Post'}
    in_progress = {
        'Top Projects Chosen', 'Research + Brainstorm', 'Outline',
        'Script Development', 'Script: Visual Map', 'Ready to Film',
        'Filming: Production', 'VO: Production', 'VO: Revision',
        'Organizing Files > Edit', 'Hook Only', 'C1: Story', 'C2: Audio',
        'C3: Color', 'C4: Polish',
    }
    if status in published:
        return 'tg'
    if status in in_progress:
        return 'tb'
    return 'tn'


def stag(status):
    if not status:
        return '<span class="tag tn">—</span>'
    cls = status_cls(status)
    return f'<span class="tag {cls}">{status}</span>'


def query_notion(db_id, filter_payload, sorts=None):
    pages, has_more, cursor = [], True, None
    payload = {'page_size': 100, 'filter': filter_payload}
    if sorts:
        payload['sorts'] = sorts
    while has_more:
        if cursor:
            payload['start_cursor'] = cursor
        resp = requests.post(
            f'https://api.notion.com/v1/databases/{db_id}/query',
            headers=NOTION_HEADERS, json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        pages.extend(data.get('results', []))
        has_more = data.get('has_more', False)
        cursor   = data.get('next_cursor')
    return pages


def fetch_pipeline_tab():
    statuses = [
        'Top Projects Chosen', 'Research + Brainstorm', 'Outline',
        'Script Development', 'Script: Visual Map', 'Ready to Film',
        'Filming: Production', 'VO: Production', 'VO: Revision',
        'Organizing Files > Edit',
    ]
    flt = {'or': [{'property': 'PL Status', 'select': {'equals': s}} for s in statuses]}
    return query_notion(NOTION_PIPELINE_DB_ID, flt)


def fetch_editor_tab():
    phases = ['Hook Only', 'C1: Story', 'C2: Audio', 'C3: Color', 'C4: Polish']
    flt = {'or': [{'property': 'Edit Phase', 'select': {'equals': p}} for p in phases]}
    sorts = [{'property': 'Next Cut Due', 'direction': 'ascending'}]
    return query_notion(NOTION_PIPELINE_DB_ID, flt, sorts)


def fetch_publishing_tab():
    statuses = ['Published', 'Ready to Publish', 'Scheduled Post']
    flt = {
        'and': [
            {'or': [{'property': 'PL Status', 'select': {'equals': s}} for s in statuses]},
            {'property': 'Format', 'multi_select': {'contains': 'Long form'}},
        ]
    }
    sorts = [{'property': 'Publish Date', 'direction': 'descending'}]
    return query_notion(NOTION_PIPELINE_DB_ID, flt, sorts)


def fetch_billing_tab():
    invoices = [
        'A. Full Prod Invoice', 'C. Invoice: 2025',
        'D. Invoice: 2026 (March)', 'E. Unassigned',
    ]
    flt = {'or': [{'property': 'Invoice Credit', 'select': {'equals': i}} for i in invoices]}
    return query_notion(NOTION_PIPELINE_DB_ID, flt)


def rows_pipeline(pages):
    out = []
    for p in pages:
        pr = p['properties']
        title       = prop_val(pr.get('VLOG Official Title', {}))
        pl_status   = prop_val(pr.get('PL Status', {}))
        script_st   = prop_val(pr.get('Script Status', {}))
        filming     = prop_val(pr.get('Filming Start', {}))
        pub_date    = prop_val(pr.get('Publish Date', {}))
        out.append(
            f'<tr>'
            f'<td style="font-weight:500;max-width:220px">{title or "—"}</td>'
            f'<td>{stag(pl_status)}</td>'
            f'<td><span class="mono" style="font-size:11px">{script_st or "—"}</span></td>'
            f'<td class="mono" style="color:rgba(240,239,232,0.4);font-size:11px">{filming or "—"}</td>'
            f'<td class="mono" style="color:rgba(240,239,232,0.4);font-size:11px">{pub_date or "—"}</td>'
            f'</tr>'
        )
    return '\n'.join(out)


def rows_editor(pages):
    out = []
    for p in pages:
        pr = p['properties']
        title      = prop_val(pr.get('VLOG Official Title', {}))
        phase      = prop_val(pr.get('Edit Phase', {}))
        progress   = prop_val(pr.get('Edit: Progress', {}))
        cut_due    = prop_val(pr.get('Next Cut Due', {}))
        pub_date   = prop_val(pr.get('Publish Date', {}))
        due_color  = 'var(--amber)' if cut_due else 'rgba(240,239,232,0.4)'
        out.append(
            f'<tr>'
            f'<td style="font-weight:500;max-width:220px">{title or "—"}</td>'
            f'<td>{stag(phase)}</td>'
            f'<td><span class="mono" style="font-size:11px">{progress or "—"}</span></td>'
            f'<td class="mono" style="color:{due_color};font-size:11px">{cut_due or "—"}</td>'
            f'<td class="mono" style="color:rgba(240,239,232,0.4);font-size:11px">{pub_date or "—"}</td>'
            f'</tr>'
        )
    return '\n'.join(out)


def rows_publishing(pages):
    out = []
    for p in pages:
        pr = p['properties']
        title    = prop_val(pr.get('VLOG Official Title', {}))
        pub_date = prop_val(pr.get('Publish Date', {}))
        yt_link  = prop_val(pr.get('YT Link', {}))
        views    = prop_val(pr.get('Views', {}))
        pl_st    = prop_val(pr.get('PL Status', {}))
        season   = prop_val(pr.get('Season & Show', {}))
        yt_cell  = (f'<a href="{yt_link}" target="_blank" style="color:var(--teal);font-size:11px">↗ YouTube</a>'
                    if yt_link else '<span style="color:rgba(240,239,232,0.25)">—</span>')
        try:
            views_fmt = f'{int(float(views)):,}' if views else '—'
        except (ValueError, TypeError):
            views_fmt = views or '—'
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
    return '\n'.join(out)


def rows_billing(pages):
    out = []
    for p in pages:
        pr = p['properties']
        title    = prop_val(pr.get('VLOG Official Title', {}))
        invoice  = prop_val(pr.get('Invoice Credit', {}))
        payment  = prop_val(pr.get('Edits Payment', {}))
        progress = prop_val(pr.get('Edit: Progress', {}))
        credits  = prop_val(pr.get('Credit Count', {}))
        season   = prop_val(pr.get('Season & Show', {}))
        try:
            pay_fmt = f'${float(payment):,.2f}' if payment else '—'
        except (ValueError, TypeError):
            pay_fmt = payment or '—'
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
    return '\n'.join(out)


def patch_notion_tabs(html, pipeline_p, editor_p, publishing_p, billing_p):
    import re

    def replace_tbody(html, tbody_id, rows):
        empty = f'<td colspan="6" style="color:rgba(240,239,232,0.25);text-align:center;padding:20px">Fetching from Notion…</td>'
        empty5 = f'<td colspan="5" style="color:rgba(240,239,232,0.25);text-align:center;padding:20px">Fetching from Notion…</td>'
        return re.sub(
            rf'(<tbody id="{tbody_id}">).*?(</tbody>)',
            lambda m: m.group(1) + rows + m.group(2),
            html, flags=re.DOTALL
        )

    def replace_count(html, elem_id, text):
        return re.sub(
            rf'(<span[^>]+id="{elem_id}"[^>]*>)[^<]*(</span>)',
            rf'\g<1>{text}\g<2>',
            html
        )

    pipeline_rows = rows_pipeline(pipeline_p)
    editor_rows   = rows_editor(editor_p)
    pub_rows      = rows_publishing(publishing_p)
    billing_rows  = rows_billing(billing_p)

    html = replace_tbody(html, 'pipeline-tbody', pipeline_rows)
    html = replace_tbody(html, 'editor-tbody',   editor_rows)
    html = replace_tbody(html, 'publishing-tbody', pub_rows)
    html = replace_tbody(html, 'billing-tbody',  billing_rows)

    html = replace_count(html, 'pipeline-count', f'{len(pipeline_p)} projects')
    html = replace_count(html, 'editor-count',   f'{len(editor_p)} in edit')
    html = replace_count(html, 'publishing-count', f'{len(publishing_p)} published')
    html = replace_count(html, 'billing-count',  f'{len(billing_p)} invoices')

    return html

# ── HTML Patching ─────────────────────────────────────────────────────────────
def patch(html, pattern, repl, label):
    new, n = re.subn(pattern, repl, html)
    return new, (label, n > 0)


def patch_html(html, s):
    """Apply all patches and return (patched_html, list_of_(label, ok))."""
    results = []

    def apply(label, pattern, repl):
        nonlocal html
        html, r = patch(html, pattern, repl, label)
        results.append(r)

    lf          = s['lf_count']
    subs        = s['channel_subs']
    wt          = int(s['watch_hours'])
    tv          = s['total_views']
    ts          = s['total_subs']
    ar          = s['avg_retention']
    gate        = 1000 if subs < 1000 else 10000
    subs_to_gate = max(0, gate - subs)
    subs_pct    = min(100, round(subs / gate * 100))
    lf_pct      = min(100, lf)
    wt_pct      = min(100, round(wt / 4000 * 100))

    # ── Home: metric tile values (have IDs) ───────────────────────────────
    apply('h-lf value',
          r'(id="h-lf">)\d[\d,]*(</)',
          rf'\g<1>{lf}\g<2>')

    apply('h-lf-rem text',
          r'(id="h-lf-rem">)\d+ remaining(</span>)',
          rf'\g<1>{100 - lf} remaining\g<2>')

    apply('h-lf-bar width',
          r'(background:var\(--amber\);width:)\d+%(" id="h-lf-bar")',
          rf'\g<1>{lf_pct}%\g<2>')

    apply('h-subs value',
          r'(id="h-subs">)[\d,]+(</)',
          rf'\g<1>{fmt(subs)}\g<2>')

    apply('h-subs-rem text',
          r'(id="h-subs-rem">)[\d,]+ to gate 1(</span>)',
          rf'\g<1>{fmt(subs_to_gate)} to gate 1\g<2>')

    apply('h-subs-bar width',
          r'(background:var\(--amber\);width:)[\d.]+%(" id="h-subs-bar")',
          rf'\g<1>{subs_pct}%\g<2>')

    apply('h-wt value',
          r'(id="h-wt">)[\d,]+(</)',
          rf'\g<1>{fmt(wt)}\g<2>')

    apply('h-wt sub remaining',
          r'(/ 4,000 hrs · )[\d,]+ remaining(</div>)',
          rf'\g<1>{fmt(4000 - wt)} remaining\g<2>')

    # ── Home: performance card ────────────────────────────────────────────
    apply('perf avg retention value',
          r'(>Avg Retention</span><span[^>]+>)[\d.]+%(</span>)',
          rf'\g<1>{ar:.1f}%\g<2>')

    apply('perf avg retention bar',
          r'(background:var\(--teal\);width:)[\d.]+%(">\s*</div></div>\s*<div[^>]+>avg across 8 S1)',
          rf'\g<1>{ar:.1f}%\g<2>')

    # ── Scoreboard: long form ─────────────────────────────────────────────
    apply('sb-lf-txt',
          r'(id="sb-lf-txt">)\d+ / 100(</span>)',
          rf'\g<1>{lf} / 100\g<2>')

    apply('sb-lf-bar width',
          r'(background:var\(--amber\);width:)\d+%(" id="sb-lf-bar")',
          rf'\g<1>{lf_pct}%\g<2>')

    apply('sb-lf-rem',
          r'(id="sb-lf-rem">)\d+ to go(</div>)',
          rf'\g<1>{100 - lf} to go\g<2>')

    apply('inp-lf value',
          r'(id="inp-lf" value=")\d+(")',
          rf'\g<1>{lf}\g<2>')

    # ── Scoreboard: subscribers ───────────────────────────────────────────
    apply('sb-sub-txt',
          r'(id="sb-sub-txt">)[\d,]+ / [\d,]+(</span>)',
          rf'\g<1>{fmt(subs)} / {fmt(gate)}\g<2>')

    apply('sb-sub-bar width',
          r'(background:var\(--amber\);width:)[\d.]+%(" id="sb-sub-bar")',
          rf'\g<1>{subs_pct}%\g<2>')

    apply('sb-sub-rem',
          r'(id="sb-sub-rem">)[\d,]+ to gate(</div>)',
          rf'\g<1>{fmt(subs_to_gate)} to gate\g<2>')

    apply('inp-subs value',
          r'(id="inp-subs" value=")\d+(")',
          rf'\g<1>{subs}\g<2>')

    # ── Scoreboard: watch time ────────────────────────────────────────────
    apply('sb-wt-txt',
          r'(id="sb-wt-txt">)[\d,]+ / 4,000(</span>)',
          rf'\g<1>{fmt(wt)} / 4,000\g<2>')

    apply('sb-wt-bar width',
          r'(background:var\(--amber\);width:)\d+%(" id="sb-wt-bar")',
          rf'\g<1>{wt_pct}%\g<2>')

    apply('sb-wt-rem',
          r'(id="sb-wt-rem">)[\d,]+ remaining · \d+%(</div>)',
          rf'\g<1>{fmt(4000 - wt)} remaining · {wt_pct}%\g<2>')

    apply('inp-wt value',
          r'(id="inp-wt" value=")\d+(")',
          rf'\g<1>{wt}\g<2>')

    # ── Videos tab: S1 aggregate tiles ───────────────────────────────────
    apply('S1 total views',
          r'(Total Views</div><div class="mono"[^>]+>)[\d,]+(</div>)',
          rf'\g<1>{fmt(tv)}\g<2>')

    apply('S1 watch hrs',
          r'(Watch Hrs</div><div class="mono"[^>]+>)[\d,]+(</div>)',
          rf'\g<1>{fmt(wt)}\g<2>')

    apply('S1 subs gained',
          r'(Subs Gained</div><div class="mono"[^>]+>)\+[\d,]+(</div>)',
          rf'\g<1>+{fmt(ts)}\g<2>')

    apply('S1 avg retention tile',
          r'(Avg Retention</div><div class="mono" style="font-size:18px;font-weight:600">)[\d.]+%(</div>)',
          rf'\g<1>{ar:.1f}%\g<2>')

    return html, results



def extract_lf_vid_ids(html):
    m = re.search(r'const LF=\[(.*?)\];', html, re.DOTALL)
    return re.findall(r'vid:"([A-Za-z0-9_-]{11})"', m.group(1)) if m else []


def patch_lf_titles(html, titles):
    def replacer(m):
        vid_id = m.group(2)
        if vid_id in titles:
            new_title = titles[vid_id].replace('"', '\\"')
            return m.group(1) + new_title + '"'
        return m.group(0)
    return re.sub(r'(vid:"([A-Za-z0-9_-]{11})"[^}]*?t:")([^"]*)"', replacer, html)

# ── Git push ──────────────────────────────────────────────────────────────────


def git_push(message):
    remote = f'https://{GH_TOKEN}@github.com/xavycarc-jpg/xvc-dashboard.git'
    subprocess.run(['git', 'remote', 'set-url', 'origin', remote],
                   cwd=DASHBOARD_DIR, check=True, capture_output=True)
    subprocess.run(['git', 'add', 'index.html'],
                   cwd=DASHBOARD_DIR, check=True, capture_output=True)
    r = subprocess.run(['git', 'commit', '-m', message],
                       cwd=DASHBOARD_DIR, capture_output=True, text=True)
    if 'nothing to commit' in r.stdout + r.stderr:
        return 'nothing to commit — dashboard already up to date'
    subprocess.run(['git', 'push', 'origin', 'main'],
                   cwd=DASHBOARD_DIR, check=True, capture_output=True)
    return 'pushed to main ✓'


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print('─' * 56)
    print('  XVC Dashboard Updater')
    print('─' * 56)

    print('\n[1/6] Authenticating with Google...')
    creds   = get_credentials()
    youtube = build('youtube', 'v3', credentials=creds)
    yta     = build('youtubeAnalytics', 'v2', credentials=creds)

    print('\n[2/6] Fetching YouTube channel data...')
    ch      = get_channel_info(youtube)
    all_ids = get_all_video_ids(youtube, ch['uploads_playlist'])
    lf_ids, sf_ids = classify_videos(youtube, all_ids)
    print(f'  Channel subscribers : {ch["subs"]:,}')
    print(f'  Total videos        : {len(all_ids)}')
    print(f'  Long form (>62s)    : {len(lf_ids)}')
    print(f'  Short form (≤62s)   : {len(sf_ids)}')

    print('\n[3/6] Fetching S1 video IDs from Notion...')
    s1_ids = get_s1_video_ids()
    print(f'  S1 records with YT Link: {len(s1_ids)}')
    for vid in s1_ids:
        print(f'    {vid}')

    print('\n[4/6] Fetching YouTube Analytics...')
    analytics = get_analytics(yta, all_ids)
    s1_data   = {vid: analytics[vid] for vid in s1_ids if vid in analytics}
    print(f'  Analytics rows total  : {len(analytics)}')
    print(f'  S1 rows with data     : {len(s1_data)}')

    if not s1_data:
        print('  ERROR: no S1 analytics data — aborting.')
        return

    total_views   = sum(v['views']         for v in s1_data.values())
    total_subs    = sum(v['subs']           for v in s1_data.values())
    watch_minutes = sum(v['watch_minutes']  for v in s1_data.values())
    watch_hours   = watch_minutes / 60
    avg_retention = sum(v['avg_pct']        for v in s1_data.values()) / len(s1_data)
    avg_avd_secs  = sum(v['avg_dur_secs']   for v in s1_data.values()) / len(s1_data)

    stats = {
        'lf_count':     len(lf_ids),
        'sf_count':     len(sf_ids),
        'channel_subs': ch['subs'],
        'total_views':  total_views,
        'total_subs':   total_subs,
        'watch_hours':  watch_hours,
        'avg_retention': avg_retention,
        'avg_avd_secs': avg_avd_secs,
    }

    print('\n── Calculated stats ─────────────────────────────────')
    print(f'  Long form published  : {stats["lf_count"]}')
    print(f'  Channel subscribers  : {stats["channel_subs"]:,}')
    print(f'  S1 total views       : {stats["total_views"]:,}')
    print(f'  S1 subs gained       : {stats["total_subs"]:,}')
    print(f'  S1 watch hours       : {stats["watch_hours"]:.1f} hrs')
    print(f'  S1 avg retention     : {stats["avg_retention"]:.1f}%')
    print(f'  S1 avg AVD           : {mmss(stats["avg_avd_secs"])}')
    print('─' * 56)

    print('\n[4b/6] Fetching Notion pipeline data...')
    import traceback
    notion_tabs_ok = False
    pipeline_pages = editor_pages = publishing_pages = billing_pages = []
    try:
        print(f'  DB ID      : {NOTION_PIPELINE_DB_ID}')
        print(f'  Token set  : {bool(NOTION_TOKEN)}')

        # ── Deep diagnostics: confirm the ID type ────────────────────
        candidate = NOTION_PIPELINE_DB_ID
        base = 'https://api.notion.com/v1'

        r_db = requests.get(f'{base}/databases/{candidate}', headers=NOTION_HEADERS)
        print(f'  GET /databases/{candidate}')
        print(f'    status : {r_db.status_code}')
        print(f'    body   : {r_db.text[:500]}')

        r_pg = requests.get(f'{base}/pages/{candidate}', headers=NOTION_HEADERS)
        print(f'  GET /pages/{candidate}')
        print(f'    status : {r_pg.status_code}')
        print(f'    body   : {r_pg.text[:500]}')

        if r_pg.status_code == 200:
            print('  → ID is a PAGE — listing child blocks to find the embedded database ...')
            r_ch = requests.get(f'{base}/blocks/{candidate}/children', headers=NOTION_HEADERS)
            print(f'    children status : {r_ch.status_code}')
            if r_ch.status_code == 200:
                for blk in r_ch.json().get('results', []):
                    print(f'    block {blk["id"]}  type={blk["type"]}')
            else:
                print(f'    children body : {r_ch.text[:400]}')

        print('  Searching all databases accessible to this integration ...')
        r_s = requests.post(f'{base}/search', headers=NOTION_HEADERS,
            json={'filter': {'value': 'database', 'property': 'object'}, 'page_size': 20})
        print(f'    search status : {r_s.status_code}')
        if r_s.status_code == 200:
            for obj in r_s.json().get('results', []):
                title = ''.join(t.get('plain_text','') for t in obj.get('title',[]))
                print(f'    db {obj["id"]}  title={title!r}')
        else:
            print(f'    search body : {r_s.text[:400]}')
        # ─────────────────────────────────────────────────────────────

        print('  → Pipeline tab ...')
        pipeline_pages = fetch_pipeline_tab()
        print(f'    {len(pipeline_pages)} rows returned')
        if pipeline_pages:
            keys = list(pipeline_pages[0]['properties'].keys())
            print(f'    Property keys on first row: {keys}')
            first_title = pipeline_pages[0]['properties'].get('VLOG Official Title', {})
            print(f'    First title raw: {first_title}')

        print('  → Editor tab ...')
        editor_pages = fetch_editor_tab()
        print(f'    {len(editor_pages)} rows returned')

        print('  → Publishing tab ...')
        publishing_pages = fetch_publishing_tab()
        print(f'    {len(publishing_pages)} rows returned')

        print('  → Billing tab ...')
        billing_pages = fetch_billing_tab()
        print(f'    {len(billing_pages)} rows returned')

        notion_tabs_ok = True
        print('  Notion fetch OK')
    except Exception as e:
        print(f'  ✗ Notion pipeline fetch FAILED: {e}')
        traceback.print_exc()

    print('\n[5/6] Patching index.html...')
    with open(DASHBOARD_FILE, 'r', encoding='utf-8') as f:
        html = f.read()

    lf_vid_ids = extract_lf_vid_ids(html)
    if lf_vid_ids:
        print(f'  Fetching live titles for {len(lf_vid_ids)} LF videos...')
        lf_titles = get_video_titles(youtube, lf_vid_ids)
        html = patch_lf_titles(html, lf_titles)
        for vid in lf_vid_ids:
            print(f'    {vid} -> {lf_titles.get(vid, "(not found)")}')

    html, patch_results = patch_html(html, stats)

    if notion_tabs_ok:
        # Diagnostic: show HTML around each tbody before replacement
        for tid in ['pipeline-tbody', 'editor-tbody', 'publishing-tbody', 'billing-tbody']:
            m = re.search(rf'<tbody id="{tid}">(.*?)</tbody>', html, re.DOTALL)
            if m:
                snippet = m.group(0)[:120].replace('\n', ' ')
                print(f'  PRE  [{tid}]: {snippet!r}')
            else:
                print(f'  PRE  [{tid}]: NOT FOUND IN HTML')

        html = patch_notion_tabs(html, pipeline_pages, editor_pages, publishing_pages, billing_pages)

        # Diagnostic: show HTML around each tbody after replacement
        for tid in ['pipeline-tbody', 'editor-tbody', 'publishing-tbody', 'billing-tbody']:
            m = re.search(rf'<tbody id="{tid}">(.*?)</tbody>', html, re.DOTALL)
            if m:
                snippet = m.group(0)[:120].replace('\n', ' ')
                print(f'  POST [{tid}]: {snippet!r}')
            else:
                print(f'  POST [{tid}]: NOT FOUND')
        print('  ✓ Notion tabs patched')

    ok_count   = sum(1 for _, ok in patch_results if ok)
    fail_count = sum(1 for _, ok in patch_results if not ok)

    for label, ok in patch_results:
        print(f'  {"✓" if ok else "✗"} {label}')

    print(f'\n  {ok_count} patches applied, {fail_count} no-match')

    with open(DASHBOARD_FILE, 'w', encoding='utf-8') as f:
        f.write(html)

    print('\n[6/6] Pushing to GitHub...')
    commit_msg = (
        f'Auto-update {date.today()} — '
        f'views:{total_views:,} subs:{ch["subs"]:,} wt:{watch_hours:.0f}h'
    )
    result = git_push(commit_msg)
    print(f'  {result}')

    
    # [7/7] Slack notifications
    print('[7/7] Checking Slack notifications...')
    try:
        import slack_notify
        milestone_hits = check_milestones(video_stats)
        for title, milestone in milestone_hits:
            slack_notify.notify_milestone(title, milestone)
        new_videos = check_new_publishes(video_stats)
        for v in new_videos:
            slack_notify.notify_publish(v['title'], f"https://youtube.com/watch?v={v['id']}")
        save_state(video_stats)
        print(f'  State saved. {len(milestone_hits)} milestone(s), {len(new_videos)} new publish(es).')
    except Exception as e:
        print(f'  Slack step skipped: {e}')

    print(f'\n✓  Done. Live at https://xavycarc-jpg.github.io/xvc-dashboard/')
    print('─' * 56)


if __name__ == '__main__':
    main()
