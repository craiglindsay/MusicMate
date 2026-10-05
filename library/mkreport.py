"""Build the review dashboard artifact from the live database."""
import sqlite3, json, collections, html, os, re
import incomplete, merge

DB = 'library.db'
OUT = 'library-review.html'

def esc(s): return html.escape(str(s) if s is not None else '')

def split_path(p):
    """Render a path as library / artist / album / file for scanning."""
    parts = p.split('/')
    return parts[0], '/'.join(parts[1:-1]), parts[-1]

def gather():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    d = {}
    aud = "kind='audio'"
    d['totals'] = dict(
        tracks=c.execute(f'SELECT COUNT(*) FROM files WHERE {aud}').fetchone()[0],
        gb=c.execute(f'SELECT SUM(size) FROM files WHERE {aud}').fetchone()[0] / 1e9,
        albums=c.execute(f'SELECT COUNT(DISTINCT folder) FROM files WHERE {aud}').fetchone()[0],
    )
    d['bysrc'] = [dict(src=r[0], n=r[1], gb=r[2] / 1e9) for r in c.execute(
        f'SELECT src,COUNT(*),SUM(size) FROM files WHERE {aud} GROUP BY src')]
    d['itunes_left'] = c.execute(
        f'SELECT COUNT(*) FROM files WHERE {aud} AND path LIKE "VariousArtists/Itunes Matched/%"'
    ).fetchone()[0]
    d['moves'] = dict(c.execute('SELECT op,COUNT(*) FROM moves GROUP BY op').fetchall())
    try:
        d['canon'] = c.execute('SELECT COUNT(*) FROM mb_artist WHERE mb_name IS NOT NULL').fetchone()[0]
    except Exception:
        d['canon'] = 0
    d['tagfix'] = c.execute('SELECT COUNT(*) FROM tag_backup').fetchone()[0]
    for f in ('artist', 'albumartist', 'album', 'title', 'track', 'year', 'genre'):
        d.setdefault('tagcov', {})[f] = c.execute(
            f'SELECT COUNT(*) FROM files WHERE {aud} AND {f} IS NOT NULL AND {f}!=""'
        ).fetchone()[0]

    inc = incomplete.analyse(c)
    d['incomplete'] = sorted([r for r in inc if r['verdict'] == 'incomplete'],
                             key=lambda r: -r['confidence'])
    d['stray'] = [r for r in inc if r['verdict'] in ('stray-or-single', 'untracked')]
    d['boxset'] = [r for r in inc if r['verdict'] == 'likely-boxset']

    dups = []
    for (f,) in c.execute(f'SELECT DISTINCT folder FROM files WHERE {aud}'):
        rs = c.execute(f'SELECT track,title,album FROM files WHERE folder=? AND {aud} '
                       f'AND track IS NOT NULL', (f,)).fetchall()
        ts = [r['track'] for r in rs]
        if ts and len(ts) != len(set(ts)):
            albums = sorted({r['album'] for r in rs if r['album']})
            cnt = collections.Counter(ts)
            dups.append(dict(folder=f, n=len(rs), n_albums=len(albums),
                             albums=albums[:3],
                             repeated=sorted(k for k, v in cnt.items() if v > 1)))
    d['dupnum'] = sorted(dups, key=lambda r: -r['n_albums'])

    d['damaged'] = [dict(path=r[0], err=(r[1] or '')[:70], dur=r[2] or 0)
                    for r in c.execute('SELECT path,fp_err,duration FROM files '
                                       'WHERE fp_err IS NOT NULL ORDER BY path')]
    plan = json.load(open('mergeplan.json', encoding='utf-8'))
    d['review'] = [x for g in plan['groups'] for x in g['decisions']
                   if x['confidence'] != 'auto']
    d['itunes'] = [dict(folder=r[0], n=r[1]) for r in c.execute(
        f'SELECT folder,COUNT(*) FROM files WHERE {aud} AND '
        f'path LIKE "VariousArtists/Itunes Matched/%" GROUP BY folder '
        f'ORDER BY COUNT(*) DESC')]
    c.close()
    return d

# ------------------------------------------------------------------ rendering
def path_cell(p):
    lib, mid, fn = split_path(p)
    return (f'<span class="p-lib">{esc(lib)}</span>'
            f'<span class="p-mid">{esc(mid)}</span>'
            f'<span class="p-fn">{esc(fn)}</span>')

def rows_incomplete(d):
    out = []
    for r in d['incomplete']:
        sev = 'crit' if r['confidence'] >= 0.9 else ('warn' if r['confidence'] >= 0.8 else 'note')
        out.append(f'<tr data-s="{sev}"><td><span class="chip {sev}">'
                   f'{r["confidence"]:.2f}</span></td>'
                   f'<td class="path">{path_cell(r["folder"])}</td>'
                   f'<td class="num">{r["n"]}</td>'
                   f'<td class="detail">{esc(r["detail"])}</td></tr>')
    return '\n'.join(out)

def rows_dup(d):
    out = []
    for r in d['dupnum']:
        sev = 'crit' if r['n_albums'] > 1 else 'warn'
        alb = ', '.join(esc(a) for a in r['albums'] if a) or '<i>untagged</i>'
        out.append(f'<tr data-s="{sev}"><td><span class="chip {sev}">'
                   f'{r["n_albums"] or "?"}</span></td>'
                   f'<td class="path">{path_cell(r["folder"])}</td>'
                   f'<td class="num">{r["n"]}</td>'
                   f'<td class="detail">repeats {esc(r["repeated"][:10])}'
                   f'<br><span class="sub">album tags: {alb}</span></td></tr>')
    return '\n'.join(out)

def rows_review(d):
    out = []
    for r in d['review']:
        sev = 'warn' if r['match'] == 'fingerprint' else 'note'
        dist = f'{r["dist"]:.3f}' if r.get('dist') is not None else '--'
        out.append(f'<tr data-s="{sev}"><td><span class="chip {sev}">{esc(r["match"])}</span>'
                   f'<br><span class="sub mono">{dist}</span></td>'
                   f'<td class="path">{path_cell(r["keep"])}'
                   f'<div class="vs">vs</div>{path_cell(r["drop"])}</td>'
                   f'<td class="num">{(r["keep_br"] or 0)//1000}k<br>'
                   f'<span class="sub">{(r["drop_br"] or 0)//1000}k</span></td>'
                   f'<td class="detail">{esc(r["why"])}</td></tr>')
    return '\n'.join(out)

def rows_damaged(d):
    out = []
    for r in d['damaged']:
        out.append(f'<tr data-s="crit"><td><span class="chip crit">decode</span></td>'
                   f'<td class="path">{path_cell(r["path"])}</td>'
                   f'<td class="num">{r["dur"]:.0f}s</td>'
                   f'<td class="detail mono sub">{esc(r["err"])}</td></tr>')
    return '\n'.join(out)

def rows_itunes(d):
    out = []
    for r in d['itunes']:
        out.append(f'<tr data-s="note"><td><span class="chip note">{r["n"]}</span></td>'
                   f'<td class="path">{path_cell(r["folder"])}</td>'
                   f'<td class="num">{r["n"]}</td>'
                   f'<td class="detail">no counterpart in the main tree - needs a home</td></tr>')
    return '\n'.join(out)

def rows_stray(d):
    out = []
    for r in d['stray'] + d['boxset']:
        sev = 'note'
        out.append(f'<tr data-s="{sev}"><td><span class="chip note">'
                   f'{esc(r["verdict"].split("-")[0])}</span></td>'
                   f'<td class="path">{path_cell(r["folder"])}</td>'
                   f'<td class="num">{r["n"]}</td>'
                   f'<td class="detail">{esc(r["detail"])}</td></tr>')
    return '\n'.join(out)

TABS = [
    ('incomplete', 'Incomplete albums', 'Tracks are missing. Highest confidence first.',
     ['Conf', 'Album folder', 'Tracks', 'What is missing'], rows_incomplete),
    ('dup', 'Two releases in one folder', 'Track numbers repeat because the folder holds more than one release. Splitting these needs your call - the tags are not wrong.',
     ['Albums', 'Folder', 'Files', 'Evidence'], rows_dup),
    ('review', 'Held for review', 'Matched on metadata rather than sound, or too close to call acoustically. Nothing here was moved.',
     ['Match', 'Kept / dropped', 'kbps', 'Reasoning'], rows_review),
    ('damaged', 'Damaged files', 'These fail to decode cleanly. Some may now have a healthy twin elsewhere in the library.',
     ['Fault', 'File', 'Length', 'Decoder error'], rows_damaged),
    ('itunes', 'iTunes Matched leftovers', 'Real music with no counterpart in the main tree. Needs relocating, not deleting.',
     ['Tracks', 'Folder', 'n', 'Note'], rows_itunes),
    ('stray', 'Singles and strays', 'Small folders and box sets - mostly fine, listed for completeness.',
     ['Kind', 'Folder', 'Tracks', 'Detail'], rows_stray),
]

def build():
    d = gather()
    nav, panes = [], []
    for i, (key, title, blurb, heads, fn) in enumerate(TABS):
        body = fn(d)
        n = body.count('<tr')
        nav.append(f'<button class="tab{" on" if i==0 else ""}" data-t="{key}">'
                   f'{esc(title)}<span class="n">{n}</span></button>')
        head = ''.join(f'<th>{esc(h)}</th>' for h in heads)
        panes.append(
            f'<section class="pane{" on" if i==0 else ""}" id="p-{key}">'
            f'<p class="blurb">{esc(blurb)}</p>'
            f'<div class="tw"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table></div></section>')

    t = d['totals']; mv = d['moves']
    tagpct = 100 * d['tagcov']['albumartist'] / max(1, t['tracks'])
    stats = [
        ('Tracks in library', f"{t['tracks']:,}", f"{t['gb']:.0f} GB across {t['albums']:,} album folders"),
        ('Duplicates retired', f"{mv.get('merge',0):,}", 'moved, not deleted - every one reversible'),
        ('Album artist filled', f"{tagpct:.0f}%", f"was 54% before this pass"),
        ('Artist names verified', f"{d['canon']:,}", 'against MusicBrainz, with sort names'),
    ]
    stat_html = ''.join(
        f'<div class="stat"><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div>'
        f'<div class="s">{esc(s)}</div></div>' for k, v, s in stats)

    return f"""<title>Music Library Merge Review</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root {{
  --paper:#F4F6F5; --card:#FFFFFF; --ink:#171A1B; --mut:#5C6A6B;
  --mut2:#7E8C8D; --rule:#DCE3E1; --accent:#A85A16; --accent-soft:#F2E4D4;
  --ok:#2F7D5B; --warn:#9C6B0B; --crit:#A8443A;
  --ok-bg:#E2EFE8; --warn-bg:#F6EAD2; --crit-bg:#F6E0DD;
  --shadow:0 1px 2px rgba(20,30,28,.05), 0 8px 24px -12px rgba(20,30,28,.18);
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --paper:#101415; --card:#181D1E; --ink:#E6EAE8; --mut:#9AA8A8; --mut2:#7E8C8D;
    --rule:#28302F; --accent:#E0973F; --accent-soft:#3A2A18;
    --ok:#63B98D; --warn:#D6A445; --crit:#D97C70;
    --ok-bg:#172A22; --warn-bg:#2C2415; --crit-bg:#2E1C1A;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 28px -14px rgba(0,0,0,.7);
  }}
}}
:root[data-theme="dark"] {{
  --paper:#101415; --card:#181D1E; --ink:#E6EAE8; --mut:#9AA8A8; --mut2:#7E8C8D;
  --rule:#28302F; --accent:#E0973F; --accent-soft:#3A2A18;
  --ok:#63B98D; --warn:#D6A445; --crit:#D97C70;
  --ok-bg:#172A22; --warn-bg:#2C2415; --crit-bg:#2E1C1A;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 28px -14px rgba(0,0,0,.7);
}}
* {{ box-sizing:border-box; }}
body {{ background:var(--paper); color:var(--ink);
  font:400 15px/1.55 "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
  -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:1180px; margin:0 auto; padding:44px 26px 90px; }}

header {{ display:flex; flex-direction:column; gap:10px; margin-bottom:34px; }}
.eyebrow {{ font:500 11.5px/1 "IBM Plex Mono", ui-monospace, monospace;
  letter-spacing:.14em; text-transform:uppercase; color:var(--accent); }}
h1 {{ font:600 clamp(30px,4.4vw,46px)/1.05 "Fraunces", Georgia, serif;
  letter-spacing:-.015em; margin:0; text-wrap:balance; }}
.lede {{ color:var(--mut); max-width:66ch; margin:0; font-size:16px; }}

.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
  gap:14px; margin:30px 0 38px; }}
.stat {{ background:var(--card); border:1px solid var(--rule); border-radius:3px;
  padding:16px 17px; box-shadow:var(--shadow); }}
.stat .k {{ font:500 10.5px/1 "IBM Plex Mono", monospace; letter-spacing:.11em;
  text-transform:uppercase; color:var(--mut2); }}
.stat .v {{ font:600 30px/1.1 "Fraunces", Georgia, serif; margin:9px 0 5px;
  font-variant-numeric:tabular-nums; letter-spacing:-.01em; }}
.stat .s {{ font-size:12.5px; color:var(--mut); line-height:1.4; }}

.tabs {{ display:flex; flex-wrap:wrap; gap:6px; border-bottom:1px solid var(--rule);
  margin-bottom:6px; padding-bottom:0; }}
.tab {{ appearance:none; background:none; border:0; border-bottom:2px solid transparent;
  color:var(--mut); font:500 13.5px/1 "IBM Plex Sans",sans-serif; cursor:pointer;
  padding:11px 13px 12px; display:flex; align-items:center; gap:7px; margin-bottom:-1px; }}
.tab:hover {{ color:var(--ink); }}
.tab.on {{ color:var(--ink); border-bottom-color:var(--accent); }}
.tab .n {{ font:500 11px/1 "IBM Plex Mono",monospace; color:var(--mut2);
  background:var(--paper); border:1px solid var(--rule); border-radius:2px;
  padding:3px 5px; font-variant-numeric:tabular-nums; }}
.tab.on .n {{ color:var(--accent); border-color:var(--accent); }}
.tab:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}

.toolbar {{ display:flex; gap:10px; align-items:center; margin:16px 0 12px; }}
#q {{ flex:1; max-width:380px; background:var(--card); color:var(--ink);
  border:1px solid var(--rule); border-radius:3px; padding:8px 11px;
  font:400 13.5px/1.3 "IBM Plex Sans",sans-serif; }}
#q:focus {{ outline:2px solid var(--accent); outline-offset:1px; }}
.hint {{ font-size:12.5px; color:var(--mut2); }}

.blurb {{ color:var(--mut); font-size:13.5px; margin:0 0 4px; max-width:74ch; }}
.pane {{ display:none; }} .pane.on {{ display:block; }}
.tw {{ overflow-x:auto; background:var(--card); border:1px solid var(--rule);
  border-radius:3px; box-shadow:var(--shadow); }}
table {{ border-collapse:collapse; width:100%; min-width:720px; }}
th {{ text-align:left; font:500 10.5px/1 "IBM Plex Mono",monospace;
  letter-spacing:.1em; text-transform:uppercase; color:var(--mut2);
  padding:11px 13px; border-bottom:1px solid var(--rule); white-space:nowrap;
  background:var(--card); position:sticky; top:0; }}
td {{ padding:10px 13px; border-bottom:1px solid var(--rule); vertical-align:top;
  font-size:13.5px; }}
tr:last-child td {{ border-bottom:0; }}
tbody tr:hover {{ background:color-mix(in srgb, var(--accent) 5%, transparent); }}
td.num {{ font-family:"IBM Plex Mono",monospace; font-variant-numeric:tabular-nums;
  white-space:nowrap; color:var(--mut); font-size:12.5px; }}
td.detail {{ color:var(--mut); font-size:12.8px; max-width:40ch; }}
.sub {{ color:var(--mut2); font-size:11.5px; }}
.mono {{ font-family:"IBM Plex Mono",monospace; }}
.path {{ font-family:"IBM Plex Mono",monospace; font-size:12px; line-height:1.5;
  min-width:280px; }}
.p-lib {{ color:var(--mut2); }}
.p-lib::after {{ content:" / "; color:var(--rule); }}
.p-mid {{ color:var(--mut); }}
.p-mid::after {{ content:" / "; color:var(--rule); }}
.p-fn {{ color:var(--ink); }}
.vs {{ font:500 10px/1 "IBM Plex Mono",monospace; color:var(--crit);
  letter-spacing:.1em; margin:3px 0; }}
.chip {{ display:inline-block; font:500 10.5px/1 "IBM Plex Mono",monospace;
  padding:4px 6px; border-radius:2px; white-space:nowrap;
  font-variant-numeric:tabular-nums; }}
.chip.crit {{ background:var(--crit-bg); color:var(--crit); }}
.chip.warn {{ background:var(--warn-bg); color:var(--warn); }}
.chip.note {{ background:var(--ok-bg); color:var(--ok); }}
.empty {{ padding:22px; color:var(--mut2); font-size:13px; }}
footer {{ margin-top:44px; padding-top:18px; border-top:1px solid var(--rule);
  color:var(--mut2); font-size:12.5px; max-width:76ch; }}
@media (prefers-reduced-motion:reduce) {{ * {{ transition:none!important; }} }}
</style>

<div class="wrap">
<header>
  <div class="eyebrow">D:\\Music &middot; post-merge</div>
  <h1>What still needs a decision</h1>
  <p class="lede">The merge and tag pass are done and reversible. Everything below was
  deliberately left alone because it needs a judgement call rather than a rule.</p>
</header>

<div class="stats">{stat_html}</div>

<div class="tabs" role="tablist">{''.join(nav)}</div>
<div class="toolbar">
  <input id="q" type="search" placeholder="Filter by artist, album or path...">
  <span class="hint" id="cnt"></span>
</div>
{''.join(panes)}

<footer>
Quality was decided by acoustic fingerprint plus measured spectral cutoff, not by the
bitrate a file claims &mdash; VBR averages understate, and several &ldquo;320k&rdquo; files
proved to be upscales of far smaller sources. Artist names follow MusicBrainz, with the
article in the name (<span class="mono">The Beatles</span>) and the inverted form in the
sort tag (<span class="mono">Beatles, The</span>). Superseded copies sit in
<span class="mono">_superseded/</span>, swept junk in <span class="mono">_quarantine/</span>,
and every move and tag write is logged for undo.
</footer>
</div>

<script>
const tabs=[...document.querySelectorAll('.tab')], panes=[...document.querySelectorAll('.pane')];
const q=document.getElementById('q'), cnt=document.getElementById('cnt');
function current(){{ return document.querySelector('.pane.on'); }}
function filter(){{
  const v=q.value.trim().toLowerCase(); const p=current(); if(!p) return;
  let shown=0, total=0;
  p.querySelectorAll('tbody tr').forEach(tr=>{{
    total++;
    const hit = !v || tr.textContent.toLowerCase().includes(v);
    tr.hidden = !hit; if(hit) shown++;
  }});
  cnt.textContent = v ? shown+' of '+total+' rows' : total+' rows';
}}
tabs.forEach(t=>t.addEventListener('click',()=>{{
  tabs.forEach(x=>x.classList.toggle('on',x===t));
  panes.forEach(p=>p.classList.toggle('on',p.id==='p-'+t.dataset.t));
  filter();
}}));
q.addEventListener('input',filter);
filter();
</script>
"""

if __name__ == '__main__':
    open(OUT, 'w', encoding='utf-8').write(build())
    print('wrote', OUT, os.path.getsize(OUT) // 1024, 'KB')
