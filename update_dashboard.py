"""
update_dashboard.py
Pull live YouTube + Notion data, patch index.html, push to GitHub.
"""

# Auto-load .env from xvc-analytics so the script works standalone
import os as _os, pathlib as _pl
_env_candidates = [
    _pl.Path(__file__).parent.parent / 'xvc-analytics' / '.env',
    _pl.Path.home() / 'Desktop' / 'xvc-analytics' / '.env',
]
_env = next((p for p in _env_candidates if p.exists()), _env_candidates[0])
if _env.exists():
    for _ln in _env.read_text().splitlines():
        _ln = _ln.strip()
        if _ln and not _ln.startswith('#') and '=' in _ln:
            _k, _v = _ln.split('=', 1)
            _os.environ.setdefault(_k.strip(), _v.strip())

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
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ANALYTICS_DIR_CANDIDATES = [
    os.path.join(os.path.dirname(SCRIPT_DIR), 'xvc-analytics'),  # sibling of script dir
    os.path.expanduser('~/Desktop/xvc-analytics'),                # actual known location
]


def _find_cred_file(name):
    """CI (GitHub Actions) writes credentials.json/token.json into the repo
    root; local runs keep them in xvc-analytics/. Check the repo root first,
    then each known xvc-analytics location for the file itself, then fall
    back to whichever xvc-analytics directory actually exists (so a fresh
    token.json that doesn't exist yet still gets written to the right place)."""
    repo_root_path = os.path.join(SCRIPT_DIR, name)
    if os.path.exists(repo_root_path):
        return repo_root_path
    for d in _ANALYTICS_DIR_CANDIDATES:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    for d in _ANALYTICS_DIR_CANDIDATES:
        if os.path.isdir(d):
            return os.path.join(d, name)
    return os.path.join(_ANALYTICS_DIR_CANDIDATES[0], name)


CREDENTIALS_FILE = _find_cred_file('credentials.json')
TOKEN_FILE       = _find_cred_file('token.json')

NOTION_TOKEN = os.environ.get('NOTION_TOKEN')
NOTION_DB_ID   = 'dd646e89-94f2-43a3-8810-fd69f5fa8486'
NOTION_HEADERS = {
    'Authorization': f'Bearer {NOTION_TOKEN}',
    'Content-Type': 'application/json',
    'Notion-Version': '2022-06-28',
}
# Notion API 2025-09-03: databases contain data sources; rows are queried via
# /v1/data_sources/{id}/query. Legacy /databases/{id}/query is kept as fallback.
NOTION_HEADERS_DS = {**NOTION_HEADERS, 'Notion-Version': '2025-09-03'}
NOTION_KNOWN_DS_ID = '546900d5-7dbf-4c20-83d1-4953d151dde1'  # master video DB data source
NOTION_API = 'https://api.notion.com/v1'

DASHBOARD_DIR  = os.path.dirname(os.path.abspath(__file__))
# index.html is now a static renderer for data/dashboard.json (written below).
# The previous full dashboard lives on as classic.html and is still patched in place.
DASHBOARD_FILE = os.path.join(DASHBOARD_DIR, 'classic.html')
DATA_FILE      = os.path.join(DASHBOARD_DIR, 'data', 'dashboard.json')
WEEKLY_PRIORITIES_DB_ID = '3b0ce0db46974a10940f0a301d002eae'
GH_TOKEN = os.environ.get('GITHUB_TOKEN') or os.environ.get('GITHUB_PAT')

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
            # prompt='select_account' forces the Google account picker every time
            # so you can choose the correct YouTube channel account in the browser.
            creds = flow.run_local_server(
                port=0,
                prompt='select_account',
                authorization_prompt_message='Opening browser — pick the correct YouTube channel account.',
            )
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
    """Return (lf_ids, sf_ids) — public videos >60 s = long form."""
    lf, sf = [], []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = youtube.videos().list(part='contentDetails,status', id=','.join(batch)).execute()
        for item in resp['items']:
            if item.get('status', {}).get('privacyStatus') != 'public':
                continue
            m = re.search(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?',
                          item['contentDetails']['duration'])
            total = (int(m.group(1) or 0) * 3600
                     + int(m.group(2) or 0) * 60
                     + int(m.group(3) or 0))
            (lf if total > 60 else sf).append(item['id'])
    return lf, sf


def get_analytics(yta, video_ids):
    today = date.today().isoformat()
    analytics = {}
    metric_tiers = [
        (
            'views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,'
            'subscribersGained,likes,shares,comments',
            lambda row: {
                'views': int(row[1]), 'watch_minutes': float(row[2]),
                'avg_dur_secs': float(row[3]), 'avg_pct': float(row[4]),
                'subs': int(row[5]), 'likes': int(row[6]), 'shares': int(row[7]),
                'comments': int(row[8]), 'ctr': None,  # CTR/impressions not exposed by the Analytics API
            }
        ),
        (
            'views,averageViewDuration,averageViewPercentage,subscribersGained',
            lambda row: {
                'views': int(row[1]), 'watch_minutes': int(row[1]) * float(row[2]) / 60,
                'avg_dur_secs': float(row[2]), 'avg_pct': float(row[3]),
                'subs': int(row[4]), 'likes': 0, 'shares': 0, 'comments': 0, 'ctr': None,
            }
        ),
    ]
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        filters = 'video==' + ','.join(batch)
        for metrics, parsefn in metric_tiers:
            try:
                resp = yta.reports().query(
                    ids='channel==MINE',
                    startDate='2005-01-01',
                    endDate=today,
                    dimensions='video',
                    metrics=metrics,
                    filters=filters,
                ).execute()
                for row in resp.get('rows', []):
                    analytics[row[0]] = parsefn(row)
                break
            except Exception as e:
                body = getattr(e, 'content', b'')
                body = body.decode('utf-8', 'replace')[:400] if isinstance(body, bytes) else str(e)[:400]
                print(f'  Analytics tier failed ({metrics[:50]}...): {body}')
    return analytics



def youtube_summary(youtube, yta, video_ids):
    """Read-only summary for the log: channel totals, last-28-day totals,
    per-video lifetime views for recent uploads. Never raises."""
    from datetime import timedelta
    out = {}
    try:
        ch = youtube.channels().list(part='statistics', mine=True).execute()['items'][0]['statistics']
        out['channel'] = {k: int(ch.get(k, 0)) for k in ('subscriberCount', 'viewCount', 'videoCount')}
    except Exception as e:
        out['channel_error'] = str(e)[:200]
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=27)
    try:
        r = yta.reports().query(
            ids='channel==MINE', startDate=start.isoformat(), endDate=end.isoformat(),
            metrics='views,estimatedMinutesWatched,averageViewDuration,subscribersGained,subscribersLost',
        ).execute()
        row = (r.get('rows') or [[0, 0, 0, 0, 0]])[0]
        out['last28'] = {'start': start.isoformat(), 'end': end.isoformat(),
                         'views': int(row[0]), 'watch_hours': round(float(row[1]) / 60, 1),
                         'avg_view_duration_s': int(row[2]),
                         'subs_gained': int(row[3]), 'subs_lost': int(row[4])}
        r2 = yta.reports().query(
            ids='channel==MINE', startDate=start.isoformat(), endDate=end.isoformat(),
            dimensions='video', metrics='views,estimatedMinutesWatched',
            sort='-views', maxResults=10,
        ).execute()
        out['last28_top_videos'] = [{'id': x[0], 'views': int(x[1]),
                                     'watch_hours': round(float(x[2]) / 60, 1)}
                                    for x in r2.get('rows', [])]
    except Exception as e:
        body = getattr(e, 'content', b'')
        out['last28_error'] = (body.decode('utf-8', 'replace') if isinstance(body, bytes) else str(e))[:300]
    vids = []
    try:
        for i in range(0, len(video_ids), 50):
            resp = youtube.videos().list(part='snippet,statistics,contentDetails',
                                         id=','.join(video_ids[i:i + 50])).execute()
            for it in resp['items']:
                st = it.get('statistics', {})
                vids.append({'id': it['id'], 'title': it['snippet']['title'],
                             'published': it['snippet']['publishedAt'][:10],
                             'duration': it['contentDetails']['duration'],
                             'views': int(st.get('viewCount', 0)),
                             'likes': int(st.get('likeCount', 0)),
                             'comments': int(st.get('commentCount', 0))})
        vids.sort(key=lambda v: v['published'], reverse=True)
        out['_all_videos'] = vids
        out['recent_uploads'] = vids[:15]
        out['top_all_time'] = sorted(vids, key=lambda v: -v['views'])[:5]
        titles = {v['id']: v['title'] for v in vids}
        for v in out.get('last28_top_videos', []):
            v['title'] = titles.get(v['id'], '')
    except Exception as e:
        out['videos_error'] = str(e)[:200]
    return out


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
    ids = []
    for page in query_notion(NOTION_DB_ID, None):
        props  = page['properties']
        season = ((props.get('Season & Show') or {}).get('select') or {}).get('name', '')
        if not season.upper().startswith('S1'):
            continue
        yt_url = ((props.get('YT Link') or {}).get('url')) or ''
        m = re.search(r'[?&]v=([A-Za-z0-9_-]{11})', yt_url) or \
            re.search(r'youtu\.be/([A-Za-z0-9_-]{11})', yt_url)
        if m:
            ids.append(m.group(1))
    return ids



NOTION_PIPELINE_DB_ID = 'dd646e89-94f2-43a3-8810-fd69f5fa8486'


PRIORITY_ORDER = {
    "Top Priority": 0,
    "High": 1,
    "Awaiting Prod": 2,
    "Medium": 3,
    "Low": 4,
    "Completed": 5,
}

INVOICE_ORDER = {
    "A. Full Prod Invoice": 0,
    "C. Invoice: 2025": 1,
    "D. Invoice: 2026 (March)": 2,
    "E. Unassigned": 3,
}

EXCL_FILTER = {
    "property": "VLOG Official Title",
    "title": {"does_not_contain": "V1 Original v15"},
}


EXCL_TITLE_FRAGMENTS = ['v1 original v15', 'script template', 'wrong do not use']

def is_excluded(row):
    t = get_title(row).lower()
    return any(frag in t for frag in EXCL_TITLE_FRAGMENTS)


def get_priority(row):
    val = row.get("properties", {}).get("Edit: Priority", {}).get("select", {})
    name = val.get("name", "") if val else ""
    return PRIORITY_ORDER.get(name, 99)

def get_invoice(row):
    val = row.get("properties", {}).get("Invoice Credit", {}).get("select", {})
    name = val.get("name", "") if val else ""
    return INVOICE_ORDER.get(name, 99)

def get_date(row, field):
    val = row.get("properties", {}).get(field, {}).get("date", {})
    start = val.get("start", "") if val else ""
    return start or "9999-99-99"

def get_number(row, field):
    val = row.get("properties", {}).get(field, {}).get("number")
    return val if val is not None else 99999

def get_title(row):
    title_arr = row.get("properties", {}).get("VLOG Official Title", {}).get("title", [])
    return title_arr[0].get("plain_text", "") if title_arr else ""

def prop_val(prop):
    """Extract plain text from any Notion property type."""
    if not prop:
        return ''
    t = prop.get('type', '')
    if t == 'title':
        return ''.join(r['plain_text'] for r in prop.get('title', []))
    if t == 'rich_text':
        return ''.join(r['plain_text'] for r in prop.get('rich_text', []))
    if t == 'status':
        s = prop.get('status')
        return s['name'] if s else ''
    if t == 'checkbox':
        return 'Yes' if prop.get('checkbox') else ''
    if t == 'last_edited_time':
        return prop.get('last_edited_time') or ''
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
        'Currently Editing: Full', 'Post Prod + Keyword SEO', 'Editing: Hook ONLY',
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

PL_STATUS_CLASS = {
    # idea / top-level
    'Top Picks':                'pl-top',
    'Top Projects Chosen':      'pl-top',
    # research tier
    'Research ID':              'pl-research',
    'Research + Brainstorm':    'pl-research',
    'TTH ID':                   'pl-research',
    # outline / pre-prod tier
    'Outline':                  'pl-outline',
    'Pre-Prod Plan + Shot List': 'pl-outline',
    'Transfer > Mindy':         'pl-outline',
    # script tier
    'Script Dev':               'pl-script-dev',
    'Script Development':       'pl-script-dev',
    'Script RW':                'pl-script-dev',
    'Script Rewrite!!':         'pl-script-dev',
    'Script: Visual Map':       'pl-script-vm',
    # ready / filming tier
    'Ready to Film':            'pl-ready',
    'In Production':            'pl-filming',
    'Filming: Production':      'pl-filming',
    'Currently Editing: Full':  'pl-filming',
    'Editing':                  'pl-filming',
    'Editing: Hook ONLY':       'pl-filming',
    # VO / post tier
    'VO: Production':           'pl-vo-prod',
    'Post Prod + Keyword SEO':  'pl-vo-prod',
    'Packaging':                'pl-vo-prod',
    'VO: Revision':             'pl-vo-rev',
    # organize
    'Organizing Files':         'pl-org',
    'Organizing Files > Edit':  'pl-org',
}

def pl_badge(status):
    if not status:
        return '<span class="tag tn">—</span>'
    cls = PL_STATUS_CLASS.get(status, status_cls(status))
    return f'<span class="tag {cls}">{status}</span>'


_DS_CACHE = {}

def resolve_data_source(db_id):
    """Return (data_source_id, properties_schema) for a database (API 2025-09-03).
    Falls back to the known master-DB data source id."""
    if db_id in _DS_CACHE:
        return _DS_CACHE[db_id]
    ds_id = None
    r = requests.get(f'{NOTION_API}/databases/{db_id}', headers=NOTION_HEADERS_DS, timeout=30)
    if r.status_code == 200:
        sources = r.json().get('data_sources') or []
        if sources:
            ds_id = sources[0]['id']
            if len(sources) > 1:
                print(f'    note: database has {len(sources)} data sources; using the first')
    else:
        print(f'    GET /databases/{db_id} (2025-09-03) -> {r.status_code}: {r.text[:300]}')
    if not ds_id and db_id == NOTION_DB_ID:
        ds_id = NOTION_KNOWN_DS_ID
        print(f'    using known data source id {ds_id}')
    schema = {}
    if ds_id:
        r2 = requests.get(f'{NOTION_API}/data_sources/{ds_id}', headers=NOTION_HEADERS_DS, timeout=30)
        if r2.status_code == 200:
            schema = r2.json().get('properties', {})
        else:
            print(f'    GET /data_sources/{ds_id} -> {r2.status_code}: {r2.text[:300]}')
    _DS_CACHE[db_id] = (ds_id, schema)
    return ds_id, schema


def sanitize_filter(flt, schema):
    """Drop select/multi_select/status option clauses whose option no longer
    exists in the live schema (renamed/deleted options make Notion return 400)."""
    if not flt or not schema:
        return flt
    for key in ('and', 'or'):
        if key in flt:
            kids = [sanitize_filter(c, schema) for c in flt[key]]
            kids = [c for c in kids if c is not None]
            if not kids:
                return None
            return {key: kids}
    prop = flt.get('property')
    pdef = schema.get(prop)
    if pdef is None:
        print(f'    ⚠ filter property {prop!r} not in schema — clause dropped')
        return None
    ptype = pdef.get('type')
    for ftype in ('select', 'multi_select', 'status'):
        cond = flt.get(ftype)
        if not cond:
            continue
        if ptype != ftype:
            # property type changed (e.g. select -> status): re-key the clause
            if ptype in ('select', 'multi_select', 'status'):
                print(f'    ⚠ {prop!r} is now {ptype} (filter used {ftype}) — adapted')
                cond_key = next(iter(cond))
                val = cond[cond_key]
                if ptype == 'multi_select':
                    cond_key = {'equals': 'contains', 'does_not_equal': 'does_not_contain'}.get(cond_key, cond_key)
                elif ftype == 'multi_select':
                    cond_key = {'contains': 'equals', 'does_not_contain': 'does_not_equal'}.get(cond_key, cond_key)
                flt = {'property': prop, ptype: {cond_key: val}}
                ftype, cond = ptype, flt[ptype]
            else:
                print(f'    ⚠ {prop!r} is {ptype}, not {ftype} — clause dropped')
                return None
        names = {o.get('name') for o in (pdef.get(ptype, {}) or {}).get('options', [])}
        if ptype == 'status':
            names |= {g.get('name') for g in (pdef.get('status', {}) or {}).get('groups', [])}
        val = next(iter(cond.values()))
        if isinstance(val, str) and names and val not in names:
            print(f'    ⚠ option {val!r} no longer exists on {prop!r} — clause dropped')
            return None
    return flt


def _paged_query(url, headers, base_payload):
    pages, cursor = [], None
    while True:
        payload = dict(base_payload)
        if cursor:
            payload['start_cursor'] = cursor
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        if resp.status_code >= 400:
            raise requests.HTTPError(
                f'{resp.status_code} on POST {url}: {resp.text[:600]}', response=resp)
        data = resp.json()
        pages.extend(data.get('results', []))
        if not data.get('has_more'):
            return pages
        cursor = data.get('next_cursor')


def query_notion(db_id, filter_payload, sorts=None):
    """Query a Notion database via its data source (API 2025-09-03); on error
    fall back to the legacy /databases/{id}/query endpoint (2022-06-28)."""
    ds_id, schema = resolve_data_source(db_id)
    flt = sanitize_filter(filter_payload, schema) if filter_payload else None
    payload = {'page_size': 100}
    if flt:
        payload['filter'] = flt
    if sorts:
        payload['sorts'] = sorts
    if ds_id:
        try:
            return _paged_query(f'{NOTION_API}/data_sources/{ds_id}/query',
                                NOTION_HEADERS_DS, payload)
        except requests.HTTPError as e:
            print(f'    data source query failed: {e}')
            print('    falling back to legacy /databases/{id}/query')
    return _paged_query(f'{NOTION_API}/databases/{db_id}/query', NOTION_HEADERS, payload)


PIPELINE_STATUSES = [
    'IDEA: NOT A FIT', 'IDEA: Mid', 'IDEA: High', 'XVC: IDEA [UA]',
    'RTW: Location-Led', 'Top Picks', 'IDEA: Low Priority', 'YT > Substack',
    'IDEA: Not Assigned', 'Ideation: BTS Channel', 'Research ID', 'Outline',
    'TTH ID', 'Script Dev', 'Editing', 'Packaging', 'Published',
    'VO: Production', 'In Production', 'Pre-Prod Plan + Shot List',
    'Post Prod + Keyword SEO', 'Ready to Film', 'Ideation > Written',
    'Transfer > Mindy', 'Organizing Files', 'Script: Visual Map', 'MasterGrid Dev',
    'VO: Revision', 'Needs Marketing', 'Editing: Hook ONLY', 'BTS: Published',
    'Archived or Re-Purposed', 'Script RW', 'Top IG Trending', 'Scheduled',
]

def fetch_pipeline_tab():
    flt = {'and': [
        {'or': [{'property': 'PL State', 'select': {'equals': s}} for s in PIPELINE_STATUSES]},
        {'or': [{'property': 'Season & Show', 'select': {'equals': s}} for s in ['S2: Re-Invention', 'S1: Starting Over']]},
        EXCL_FILTER,
    ]}
    pipeline_raw = query_notion(NOTION_PIPELINE_DB_ID, flt)
    return pipeline_raw


def fetch_editor_tab():
    phases = ['Hook Only', 'C1: Story', 'C2: Audio', 'C3: Color', 'C4: Polish']
    flt = {'and': [
        {'or': [{'property': 'Edit Phase', 'select': {'equals': p}} for p in phases]},
        {'or': [{'property': 'Season & Show', 'select': {'equals': s}} for s in ['S2: Re-Invention', 'S1: Starting Over']]},
        EXCL_FILTER,
    ]}
    editor_raw = query_notion(NOTION_PIPELINE_DB_ID, flt)
    return editor_raw


def fetch_publishing_tab():
    statuses = ['Published', 'BTS: Published', 'Scheduled']
    flt = {
        'and': [
            {'or': [{'property': 'PL State', 'select': {'equals': s}} for s in statuses]},
            {'property': 'Format', 'multi_select': {'contains': 'Long form'}},
            {'property': 'Season & Show', 'select': {'does_not_equal': 'BTS'}},
            EXCL_FILTER,
        ]
    }
    publishing_raw = query_notion(NOTION_PIPELINE_DB_ID, flt)
    return publishing_raw


def fetch_billing_tab():
    invoices = [
        'A. Full Prod Invoice', 'C. Invoice: 2025',
        'D. Invoice: 2026 (March)', 'E. Unassigned',
    ]
    flt = {'and': [
        {'or': [{'property': 'Invoice Credit', 'select': {'equals': i}} for i in invoices]},
        EXCL_FILTER,
    ]}
    billing_raw = query_notion(NOTION_PIPELINE_DB_ID, flt)
    return billing_raw


def rows_pipeline(pages):
    out = []
    for p in pages:
        pr = p['properties']
        title       = prop_val(pr.get('VLOG Official Title', {}))
        pl_status   = prop_val(pr.get('PL State', {}))
        script_st   = prop_val(pr.get('Script', {}))
        filming     = prop_val(pr.get('Filming', {}))
        pub_date    = prop_val(pr.get('Publish', {}))
        out.append(
            f'<tr>'
            f'<td style="font-weight:500;max-width:220px">{title or "—"}</td>'
            f'<td>{pl_badge(pl_status)}</td>'
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
        pub_date   = prop_val(pr.get('Publish', {}))
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
        pub_date = prop_val(pr.get('Publish', {}))
        yt_link  = prop_val(pr.get('YT Link', {}))
        views    = prop_val(pr.get('Views', {}))
        pl_st    = prop_val(pr.get('PL State', {}))
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
    tl          = s.get('total_likes', 0)
    tsh         = s.get('total_shares', 0)
    tc          = s.get('total_comments', 0)
    ac          = s.get('avg_ctr')
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
          r'(id="sb-lf-txt">)\d+ (?:/|of) 100(</span>)',
          rf'\g<1>{lf} of 100\g<2>')

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

    apply('S1 avg ctr tile',
          r'(Avg CTR</div><div class="mono"[^>]+>)(?:[\d.]+%|—)(</div>)',
          rf'\g<1>{ac:.2f}%\g<2>' if ac is not None else r'\g<1>—\g<2>')

    apply('S1 total likes',
          r'(Total Likes</div><div class="mono"[^>]+>)[\d,]+(</div>)',
          rf'\g<1>{fmt(tl)}\g<2>')

    apply('S1 total shares',
          r'(Total Shares</div><div class="mono"[^>]+>)[\d,]+(</div>)',
          rf'\g<1>{fmt(tsh)}\g<2>')

    apply('S1 comments',
          r'(Comments</div><div class="mono"[^>]+>)[\d,]+(</div>)',
          rf'\g<1>{fmt(tc)}\g<2>')

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

# ── dashboard.json (renders the mobile index.html) ───────────────────────────
from datetime import datetime, timedelta, timezone

BALI_TZ = timezone(timedelta(hours=8))  # Asia/Makassar, no DST
STUCK_DAYS = 14
BLOCK_WORDS = ('on hold', 'blocked', 'stuck', 'paused')

STAGES = [  # (key, label, PL State values)
    ('script',  'Script',       ['Outline', 'Script Dev', 'Script Development', 'Script RW', 'Script Rewrite!!',
                                 'Script: Visual Map', 'MasterGrid Dev', 'Ideation > Written']),
    ('preprod', 'Pre-prod / Filming', ['Pre-Prod Plan + Shot List', 'Ready to Film', 'In Production',
                                 'Filming: Production', 'Transfer > Mindy']),
    ('vo',      'Voiceover',    ['VO: Production', 'VO: Revision']),
    ('edit',    'Edit',         ['Organizing Files', 'Organizing Files > Edit', 'Editing', 'Editing: Hook ONLY',
                                 'Currently Editing: Full']),
    ('package', 'Packaging / Publish', ['Packaging', 'Post Prod + Keyword SEO', 'Needs Marketing', 'Scheduled',
                                 'Ready to Publish', 'Scheduled Post']),
]
STAGE_OF = {pl: key for key, _, pls in STAGES for pl in pls}
DEFAULT_OWNER = {'script': 'Xavy', 'preprod': 'Xavy', 'vo': 'Xavy', 'edit': 'Stephan', 'package': 'Both'}
INACTIVE_EDIT_PHASES = {'', 'NA', 'Not Started'}


def bali_now():
    return datetime.now(timezone.utc).astimezone(BALI_TZ)


def latest_note(text):
    """XVC Next Steps holds dated entries, newest first ("10.15. ..."). Return the
    newest entry and its date (or None)."""
    text = (text or '').strip()
    if not text:
        return '', None
    first = re.split(r'\n(?=\s*\d{1,2}\.\d{1,2}\.?\s)', text)[0].strip()
    m = re.match(r'(\d{1,2})\.(\d{1,2})\.?\s', first)
    d = None
    if m:
        today = bali_now().date()
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
            if d > today + timedelta(days=2):
                d = date(today.year - 1, d.month, d.day)
        except ValueError:
            d = None
    return first, d


def next_step_owner(note, stage):
    x = bool(re.search(r'\bXC\b|\bXavy\b', note))
    s = bool(re.search(r'\bSE\b|\bStephan\b', note))
    if x and s:
        return 'Both'
    if x:
        return 'Xavy'
    if s:
        return 'Stephan'
    return DEFAULT_OWNER.get(stage, 'Xavy')


def page_last_edit(page):
    lt = prop_val(page['properties'].get('Last Touched', {}))
    if lt:
        return lt[:10]
    return (page.get('last_edited_time') or '')[:10]


def build_pipeline(pipeline_rows, editor_rows):
    """Active videos grouped by stage, plus an idea-bank count."""
    pages = {}
    for p in list(pipeline_rows) + list(editor_rows):
        pages[p['id']] = p
    today = bali_now().date()
    stages = {k: [] for k, _, _ in STAGES}
    ideas = 0
    for p in pages.values():
        pr = p['properties']
        pl = prop_val(pr.get('PL State', {}))
        ep = prop_val(pr.get('Edit Phase', {}))
        stage = STAGE_OF.get(pl)
        if not stage and ep not in INACTIVE_EDIT_PHASES and pl != 'Published':
            stage = 'edit'
        if not stage:
            if pl != 'Published':
                ideas += 1
            continue
        raw_note = prop_val(pr.get('XVC Next Steps', {}))
        note, note_date = latest_note(raw_note)
        last = note_date.isoformat() if note_date else page_last_edit(p)
        try:
            idle = (today - date.fromisoformat(last)).days if last else None
        except ValueError:
            idle = None
        low = note.lower()
        blocked = any(w in low for w in BLOCK_WORDS)
        stuck_reason = ''
        if blocked:
            stuck_reason = 'on hold / blocked'
        elif idle is not None and idle >= STUCK_DAYS:
            stuck_reason = f'no update in {idle} days'
        atw = (pr.get('Active This Week') or {}).get('checkbox') is True
        stages[stage].append({
            'title': prop_val(pr.get('VLOG Official Title', {})) or '(untitled)',
            'pl_state': pl,
            'edit_phase': ep if ep not in INACTIVE_EDIT_PHASES else '',
            'progress': prop_val(pr.get('Edit: Progress', {})),
            'priority': prop_val(pr.get('Edit: Priority', {})),
            'next_owner': next_step_owner(note, stage),
            'next_step': re.sub(r'^\d{1,2}\.\d{1,2}\.?\s*', '', note)[:400],
            'last_update': last or None,
            'idle_days': idle,
            'stuck': bool(stuck_reason),
            'stuck_reason': stuck_reason,
            'active_this_week': atw,
            'next_cut_due': prop_val(pr.get('Next Cut Due', {})) or None,
            'publish': prop_val(pr.get('Publish', {})) or None,
        })
    for items in stages.values():
        items.sort(key=lambda v: (not v['active_this_week'], v['stuck'],
                                  PRIORITY_ORDER.get(v['priority'], 99), v['title']))
    return {
        'stuck_days': STUCK_DAYS,
        'stages': [{'key': k, 'label': lbl, 'items': stages[k]} for k, lbl, _ in STAGES],
        'idea_bank_count': ideas,
    }


def fetch_weekly_priorities(video_titles):
    """Rows of the Weekly Priorities DB for the current (Bali) week. Never raises."""
    today = bali_now().date()
    monday = today - timedelta(days=today.weekday())
    out = {'status': 'ok', 'week_of': monday.isoformat(), 'items': [], 'note': ''}
    try:
        r = requests.get(f'{NOTION_API}/databases/{WEEKLY_PRIORITIES_DB_ID}',
                         headers=NOTION_HEADERS_DS, timeout=30)
        if r.status_code != 200:
            print(f'  ⚠ Weekly Priorities DB not reachable ({r.status_code}): {r.text[:200]}')
            print('    → share the "🎯 Weekly Priorities" database with the Notion integration')
            out['status'] = 'no_access'
            return out
        sources = r.json().get('data_sources') or []
        if not sources:
            out['status'] = 'no_access'
            return out
        ds_id = sources[0]['id']
        payload = {'page_size': 100,
                   'filter': {'and': [
                       {'property': 'Week Of', 'date': {'on_or_after': (monday - timedelta(days=7)).isoformat()}},
                       {'property': 'Week Of', 'date': {'on_or_before': (monday + timedelta(days=6)).isoformat()}},
                   ]}}
        rows = _paged_query(f'{NOTION_API}/data_sources/{ds_id}/query', NOTION_HEADERS_DS, payload)
        this_week = [p for p in rows
                     if prop_val(p['properties'].get('Week Of', {}))[:10] >= monday.isoformat()]
        if not this_week and rows:
            prev = max(prop_val(p['properties'].get('Week Of', {}))[:10] for p in rows)
            this_week = [p for p in rows if prop_val(p['properties'].get('Week Of', {}))[:10] == prev]
            out['week_of'] = prev
            out['note'] = 'No rows for this week yet; showing last week.'
        status_rank = {'Blocked': 0, 'In Progress': 1, 'Not Started': 2, 'Done': 3}
        for p in this_week:
            pr = p['properties']
            st = pr.get('Status', {}) or {}
            status = ((st.get('status') or st.get('select') or {}) or {}).get('name', '')
            vids = [video_titles.get(rel.get('id'), '') for rel in (pr.get('Video', {}) or {}).get('relation', [])]
            out['items'].append({
                'title': prop_val(pr.get('Priority', {})) or '(untitled)',
                'owner': prop_val(pr.get('Owner', {})) or 'Both',
                'status': status or 'Not Started',
                'type': prop_val(pr.get('Type', {})),
                'next_step': prop_val(pr.get('Next Step', {})),
                'last_update': prop_val(pr.get('Last Update', {})),
                'videos': [v for v in vids if v],
            })
        out['items'].sort(key=lambda i: (status_rank.get(i['status'], 9), i['type'] != 'Top Project', i['title']))
        print(f'  Weekly Priorities: {len(out["items"])} rows for week of {out["week_of"]}')
    except Exception as e:
        print(f'  ⚠ Weekly Priorities fetch failed: {e}')
        out['status'] = 'error'
    return out


def build_dashboard_data(stats, yt_summary, all_videos, lf_ids, sf_ids,
                         notion_ok, pipeline_rows, editor_rows, publishing_rows, billing_rows):
    now_utc = datetime.now(timezone.utc)
    lf_set, sf_set = set(lf_ids), set(sf_ids)
    data = {
        'generated_at': now_utc.isoformat(timespec='seconds'),
        'generated_at_bali': now_utc.astimezone(BALI_TZ).strftime('%a %d %b %Y, %H:%M') + ' (Bali)',
        'rto': {'published': stats['lf_count'], 'goal': 100},
        'channel': {
            'subscribers': stats['channel_subs'],
            'last28': yt_summary.get('last28'),
            'top_recent': (yt_summary.get('last28_top_videos') or [None])[0],
        },
        'long_form': [{'id': v['id'], 'title': v['title'], 'published': v['published'], 'views': v['views'],
                       'likes': v['likes'], 'comments': v['comments']} for v in all_videos if v['id'] in lf_set],
        'shorts': [{'id': v['id'], 'title': v['title'], 'published': v['published'], 'views': v['views'],
                    'likes': v['likes']} for v in all_videos if v['id'] in sf_set],
        'notion_ok': notion_ok,
    }
    titles = {}
    for p in list(pipeline_rows) + list(editor_rows) + list(publishing_rows) + list(billing_rows):
        titles[p['id']] = prop_val(p['properties'].get('VLOG Official Title', {}))
    data['priorities'] = fetch_weekly_priorities(titles) if NOTION_TOKEN else \
        {'status': 'error', 'week_of': None, 'items': [], 'note': 'NOTION_TOKEN missing'}
    data['pipeline'] = build_pipeline(pipeline_rows, editor_rows) if notion_ok else None
    billing = []
    for p in billing_rows:
        pr = p['properties']
        billing.append({
            'title': prop_val(pr.get('VLOG Official Title', {})),
            'invoice': prop_val(pr.get('Invoice Credit', {})),
            'payment': prop_val(pr.get('Edits Payment ', {})) or prop_val(pr.get('Edits Payment', {})),
            'progress': prop_val(pr.get('Edit: Progress', {})),
            'credits': prop_val(pr.get('Credit Count', {})),
        })
    data['billing'] = billing if notion_ok else None
    return data


def write_dashboard_json(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    tmp = DATA_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)


# ── Git push ──────────────────────────────────────────────────────────────────


def git_push(message):
    remote = f'https://{GH_TOKEN}@github.com/xavycarc-jpg/xvc-dashboard.git'
    subprocess.run(['git', 'remote', 'set-url', 'origin', remote],
                   cwd=DASHBOARD_DIR, check=True, capture_output=True)
    subprocess.run(['git', 'add', 'index.html', 'classic.html', 'data'],
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
    missing   = [vid for vid in s1_ids if vid not in analytics]
    print(f'  Analytics rows total  : {len(analytics)}')
    print(f'  S1 rows with data     : {len(s1_data)}')
    if missing:
        print(f'  ⚠ S1 IDs missing from analytics ({len(missing)}): {missing}')
        print('    (continuing — those videos will be excluded from S1 aggregates)')

    if s1_data:
        total_views    = sum(v['views']           for v in s1_data.values())
        total_subs     = sum(v['subs']             for v in s1_data.values())
        watch_minutes  = sum(v['watch_minutes']    for v in s1_data.values())
        watch_hours    = watch_minutes / 60
        avg_retention  = sum(v['avg_pct']          for v in s1_data.values()) / len(s1_data)
        avg_avd_secs   = sum(v['avg_dur_secs']     for v in s1_data.values()) / len(s1_data)
        total_likes    = sum(v.get('likes', 0)     for v in s1_data.values())
        total_shares   = sum(v.get('shares', 0)    for v in s1_data.values())
        total_comments = sum(v.get('comments', 0)  for v in s1_data.values())
        ctrs           = [v['ctr'] for v in s1_data.values() if v.get('ctr') is not None]
        avg_ctr        = sum(ctrs) / len(ctrs) if ctrs else None
    else:
        print('  ⚠ No S1 analytics data — using zeros for S1 aggregates.')
        total_views = total_subs = watch_hours = avg_retention = avg_avd_secs = 0
        total_likes = total_shares = total_comments = 0
        avg_ctr = None

    stats = {
        'lf_count':       len(lf_ids),
        'sf_count':       len(sf_ids),
        'channel_subs':   ch['subs'],
        'total_views':    total_views,
        'total_subs':     total_subs,
        'watch_hours':    watch_hours,
        'avg_retention':  avg_retention,
        'avg_avd_secs':   avg_avd_secs,
        'total_likes':    total_likes,
        'total_shares':   total_shares,
        'total_comments': total_comments,
        'avg_ctr':        avg_ctr,
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

    print('\n[4a/6] YouTube summary (read-only)...')
    yt_summary = youtube_summary(youtube, yta, all_ids)
    all_videos = yt_summary.pop('_all_videos', [])
    print('YT_SUMMARY_JSON ' + json.dumps(yt_summary, ensure_ascii=False))

    print('\n[4b/6] Fetching Notion pipeline data...')
    import traceback
    notion_tabs_ok = False
    pipeline_raw = editor_raw = publishing_raw = billing_raw = []
    try:
        print(f'  DB ID      : {NOTION_PIPELINE_DB_ID}')
        print(f'  Token set  : {bool(NOTION_TOKEN)}')

        ds_id, schema = resolve_data_source(NOTION_PIPELINE_DB_ID)
        print(f'  Data source: {ds_id}  ({len(schema)} properties)')

        print('  → Pipeline tab ...')
        pipeline_raw = fetch_pipeline_tab()
        print(f'    {len(pipeline_raw)} rows returned')
        if pipeline_raw:
            keys = list(pipeline_raw[0]['properties'].keys())
            print(f'    Property keys on first row: {keys}')
            first_title = pipeline_raw[0]['properties'].get('VLOG Official Title', {})
            print(f'    First title raw: {first_title}')

        print('  → Editor tab ...')
        editor_raw = fetch_editor_tab()
        print(f'    {len(editor_raw)} rows returned')

        print('  → Publishing tab ...')
        publishing_raw = fetch_publishing_tab()
        print(f'    {len(publishing_raw)} rows returned')

        print('  → Billing tab ...')
        billing_raw = fetch_billing_tab()
        print(f'    {len(billing_raw)} rows returned')

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
        # ── Apply exact sort + exclude logic ─────────────────────────
        pipeline_rows = [r for r in pipeline_raw
                         if not is_excluded(r)
                         and prop_val(r['properties'].get('PL State', {})) != 'Published']
        pipeline_rows.sort(key=lambda r: (
            get_number(r, 'Published Count'),
            get_date(r, 'Filming'),
            get_priority(r),
            get_date(r, 'Publish'),
        ))

        editor_rows = [r for r in editor_raw if not is_excluded(r)]
        editor_rows.sort(key=lambda r: (
            get_priority(r),
            get_date(r, 'Publish'),
            get_number(r, 'Credit Count'),
        ))

        publishing_rows = [r for r in publishing_raw if not is_excluded(r)]
        publishing_rows.sort(key=lambda r: get_date(r, 'Publish'))

        billing_rows = [r for r in billing_raw if not is_excluded(r)]
        billing_rows.sort(key=lambda r: (
            get_invoice(r),
            get_date(r, 'Publish'),
        ))

        print(f'  Pipeline rows after sort/exclude : {len(pipeline_rows)}')
        print(f'  Editor rows after sort/exclude   : {len(editor_rows)}')
        print(f'  Publishing rows after sort/exclude: {len(publishing_rows)}')
        print(f'  Billing rows after sort/exclude  : {len(billing_rows)}')

        html = patch_notion_tabs(html, pipeline_rows, editor_rows, publishing_rows, billing_rows)
        print('  ✓ Notion tabs patched')

    ok_count   = sum(1 for _, ok in patch_results if ok)
    fail_count = sum(1 for _, ok in patch_results if not ok)

    for label, ok in patch_results:
        print(f'  {"✓" if ok else "✗"} {label}')

    print(f'\n  {ok_count} patches applied, {fail_count} no-match')

    with open(DASHBOARD_FILE, 'w', encoding='utf-8') as f:
        f.write(html)

    print('\n[5b/6] Writing data/dashboard.json...')
    try:
        if not notion_tabs_ok:
            pipeline_rows = editor_rows = publishing_rows = billing_rows = []
        dash = build_dashboard_data(stats, yt_summary, all_videos, lf_ids, sf_ids, notion_tabs_ok,
                                    pipeline_rows, editor_rows, publishing_rows, billing_rows)
        write_dashboard_json(dash)
        n_active = sum(len(st['items']) for st in (dash['pipeline'] or {}).get('stages', []))
        print(f'  ✓ dashboard.json: RTO {dash["rto"]["published"]} of 100, '
              f'{len(dash["priorities"]["items"])} priorities ({dash["priorities"]["status"]}), '
              f'{n_active} active videos')
    except Exception as e:
        import traceback
        print(f'  ✗ dashboard.json NOT written: {e}')
        traceback.print_exc()

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
