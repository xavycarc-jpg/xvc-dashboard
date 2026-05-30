import os
import os
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

DASHBOARD_FILE = '/Users/xavycarc/Desktop/xvc-dashboard/index.html'
DASHBOARD_DIR  = '/Users/xavycarc/Desktop/xvc-dashboard'
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
