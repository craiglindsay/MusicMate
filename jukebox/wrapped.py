"""Wrapped (2 Nov 2026): a person's year in music, as numbers and as a DJ's voice.

stats(year, who) reads the play history in state.db (a person's own plays, or
everyone's for the household, who=0) and resolves each play's stable song key
through the read model, so a rebuild never loses a year. What counts:
listening time is every second played, finished or not; a "play" is one that
counted (state.counts_after: half the song or 4 minutes); tops rank by plays, then minutes.

narration(year, who) asks Claude for the DJ's lines, one per slide, from those
numbers, and has edge-tts speak each into cache/wrapped/<year>-<who>-<hash>/.
The hash is of the numbers, so a finished year is written once and replays
instantly, while the current year's narration follows the listening as it
grows. Without a key the lines come from templates, in the same voice.

wrapped.html is the show; /wrapped serves it, /api/wrapped/* feed it.
"""
import os, re, json, time, hashlib, sqlite3, datetime, threading, collections
from pathlib import Path

import state
import podcasts

HERE = Path(__file__).resolve().parent
DB = HERE / 'jukebox.db'
CACHE = HERE / 'cache' / 'wrapped'
VOICE = 'en-US-AndrewNeural'
_jobs = {}                 # narration key -> {'state': 'running'|'ready'|'error', ...}
_lock = threading.Lock()


def _span(year):
    a = datetime.datetime(year, 1, 1).timestamp()
    b = datetime.datetime(year + 1, 1, 1).timestamp()
    return a, b


def years(who=0):
    c = state.db()
    try:
        q = "SELECT DISTINCT CAST(strftime('%Y', started, 'unixepoch', 'localtime') AS INTEGER) y FROM play WHERE completed=1"
        args = []
        if who:
            q += ' AND person_id=?'
            args.append(who)
        return sorted((r[0] for r in c.execute(q, args)), reverse=True)
    finally:
        c.close()


def _person_name(who):
    p = podcasts.person(who) if who else None
    return p['name'] if p else 'Everyone'


def stats(year, who=0):
    a, b = _span(year)
    c = state.db()
    try:
        q = 'SELECT skey, started, seconds, completed, room FROM play WHERE started>=? AND started<?'
        args = [a, b]
        if who:
            q += ' AND person_id=?'
            args.append(who)
        plays = [dict(r) for r in c.execute(q + ' ORDER BY started', args)]
        # every year this person has heard before, for "new to you"
        q2 = 'SELECT DISTINCT artist_key FROM play WHERE completed=1 AND started<?' + (' AND person_id=?' if who else '')
        before_artists = {r[0] for r in c.execute(q2, [a] + ([who] if who else []))}
        # household share
        house_secs = c.execute('SELECT SUM(seconds) FROM play WHERE started>=? AND started<?', (a, b)).fetchone()[0] or 0
    finally:
        c.close()
    out = {'year': year, 'who': who, 'name': _person_name(who), 'generated': time.time(),
           'empty': not plays}
    if not plays:
        return out
    keys = list({p['skey'] for p in plays})
    j = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    j.row_factory = sqlite3.Row
    meta = {}
    try:
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            for r in j.execute(
                    'SELECT track.skey, track.id, track.title, track.duration, album.id album_id, album.title album, '
                    'album.year, album.decade, genre.shelf genre, artist.id artist_id, artist.name artist, '
                    "COALESCE(NULLIF(track.track_artist,''), artist.name) track_artist, album.pool "
                    'FROM track JOIN album ON album.id=track.album_id JOIN artist ON artist.id=album.artist_id '
                    'LEFT JOIN genre ON genre.id=album.genre_id WHERE track.skey IN (%s)' % ','.join('?' * len(chunk)),
                    chunk):
                meta.setdefault(r['skey'], dict(r))
        photos = {r[0] for r in j.execute("SELECT artist_id FROM artist_info WHERE image_file IS NOT NULL AND image_file<>''")}
        by_name = {}
        for r in j.execute('SELECT id, name FROM artist'):
            by_name.setdefault(r[1].lower(), r[0])
    finally:
        j.close()

    secs = sum(p['seconds'] or 0 for p in plays)
    done = [p for p in plays if p['completed'] and p['skey'] in meta]
    song_n, song_s = collections.Counter(), collections.Counter()
    art_n, art_s, alb_n, alb_s = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
    genre_s, dec_s = collections.Counter(), collections.Counter()
    month_s, hour_s, wday_s, day_s = [0] * 12, [0] * 24, [0] * 7, collections.Counter()
    rooms = collections.Counter()
    day_song = collections.Counter()
    for m in meta.values():
        perf = m['track_artist'] or m['artist']
        aid = by_name.get(perf.lower())
        if aid is None and perf == m['artist']:
            aid = m['artist_id']
        m['perf'] = perf
        m['perf_id'] = aid if aid is not None else 'n:' + perf.lower()
    for p in plays:
        m = meta.get(p['skey'])
        s = p['seconds'] or 0
        t = datetime.datetime.fromtimestamp(p['started'])
        month_s[t.month - 1] += s
        hour_s[t.hour] += s
        wday_s[t.weekday()] += s
        day_s[t.date().isoformat()] += s
        if p['room']:
            rooms[re.sub(r' \+\d+$', '', p['room'])] += s
        if not m:
            continue
        song_s[p['skey']] += s
        art_s[m['perf_id']] += s
        alb_s[m['album_id']] += s
        if m['genre']:
            genre_s[m['genre']] += s
        if m['decade']:
            dec_s[m['decade']] += s
    for p in done:
        m = meta[p['skey']]
        song_n[p['skey']] += 1
        art_n[m['perf_id']] += 1
        alb_n[m['album_id']] += 1
        day_song[(datetime.datetime.fromtimestamp(p['started']).date().isoformat(), p['skey'])] += 1

    def rank(n, s, k=5):
        return sorted(set(n) | set(s), key=lambda x: (-n[x], -s[x]))[:k]

    art_name = {}
    for m in meta.values():
        art_name[m['perf_id']] = m['perf']
    for x in [x for x in art_n if art_name.get(x, '').lower() in ('various artists', 'various', 'unknown artist')]:
        del art_n[x]
    for x in [x for x in art_s if art_name.get(x, '').lower() in ('various artists', 'various', 'unknown artist')]:
        del art_s[x]
    alb_info = {m['album_id']: m for m in meta.values()}

    top_songs = [{'id': meta[k]['id'], 'title': meta[k]['title'], 'artist': meta[k]['track_artist'],
                  'album': meta[k]['album'], 'album_id': meta[k]['album_id'], 'year': meta[k]['year'],
                  'plays': song_n[k], 'minutes': round(song_s[k] / 60)} for k in rank(song_n, song_s, 10)]
    top_artists = [{'id': x if isinstance(x, int) else None, 'name': art_name[x], 'plays': art_n[x],
                    'minutes': round(art_s[x] / 60),
                    'photo': ('/api/artist-photo/%d' % x) if x in photos else None,
                    'album_id': max((m for m in meta.values() if m['perf_id'] == x),
                                    key=lambda m: alb_s[m['album_id']])['album_id']}
                   for x in rank(art_n, art_s, 10)]
    top_albums = [{'id': x, 'title': alb_info[x]['album'], 'artist': alb_info[x]['artist'], 'year': alb_info[x]['year'],
                   'plays': alb_n[x], 'minutes': round(alb_s[x] / 60)} for x in rank(alb_n, alb_s, 5)]
    total_g = sum(genre_s.values()) or 1
    total_d = sum(dec_s.values()) or 1

    # the longest run of days with any listening
    days = sorted(day_s)
    best = run = 1
    best_end = days[0]
    for i in range(1, len(days)):
        d0 = datetime.date.fromisoformat(days[i - 1])
        d1 = datetime.date.fromisoformat(days[i])
        run = run + 1 if (d1 - d0).days == 1 else 1
        if run > best:
            best, best_end = run, days[i]
    big_day = max(day_s, key=day_s.get)
    repeat = max(day_song, key=day_song.get) if day_song else None
    first = next((p for p in done), None)

    # what kind of listener: when, how widely, how faithfully
    night = sum(hour_s[h] for h in (22, 23, 0, 1, 2, 3)) / (secs or 1)
    morning = sum(hour_s[h] for h in range(5, 10)) / (secs or 1)
    peak_hour = max(range(24), key=lambda h: hour_s[h])
    top1_share = (top_artists[0]['minutes'] * 60 / secs) if top_artists and secs else 0
    variety = len(art_s) / max(1, len(done))
    if night > .3:
        persona = ('Night Owl', 'most of your listening happened after dark')
    elif morning > .3:
        persona = ('Early Riser', 'you started a lot of days with music')
    elif top1_share > .25:
        persona = ('Superfan', 'one artist had a real hold on you')
    elif variety > .45:
        persona = ('Explorer', 'you hopped between artists like it was a sport')
    elif dec_s and max(dec_s, key=dec_s.get) <= 1980:
        persona = ('Time Traveller', 'you lived in the past, and it sounded great')
    else:
        persona = ('Loyalist', 'you know what you love and you came back for it')

    new_artists = [x for x in art_n if _artist_key(art_name[x]) not in before_artists]

    # podcasts this year
    pod = {'minutes': 0, 'episodes': 0, 'top_show': None}
    if who:
        pc = podcasts.db()
        try:
            r = pc.execute('SELECT SUM(CASE WHEN played=1 THEN duration ELSE pos END), SUM(played) FROM pod_pos '
                           'WHERE person_id=? AND updated>=? AND updated<?', (who, a, b)).fetchone()
            pod['minutes'] = round((r[0] or 0) / 60)
            pod['episodes'] = r[1] or 0
            t = pc.execute('SELECT pod_show.id, pod_show.title, COUNT(*) n FROM pod_pos JOIN pod_episode ON pod_episode.id=pod_pos.episode_id '
                           'JOIN pod_show ON pod_show.id=pod_episode.show_id WHERE pod_pos.person_id=? AND pod_pos.updated>=? '
                           'AND pod_pos.updated<? GROUP BY pod_show.id ORDER BY n DESC LIMIT 1', (who, a, b)).fetchone()
            if t:
                pod['top_show'] = {'id': t[0], 'title': t[1], 'episodes': t[2]}
        finally:
            pc.close()

    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    out.update({
        'minutes': round(secs / 60), 'hours': round(secs / 3600, 1),
        'plays': len(done), 'songs': len(song_n), 'artists': len(art_n), 'albums': len(alb_n),
        'top_songs': top_songs, 'top_artists': top_artists, 'top_albums': top_albums,
        'genres': [{'name': g, 'share': round(s / total_g, 3), 'minutes': round(s / 60)} for g, s in genre_s.most_common(6)],
        'decades': [{'decade': d, 'share': round(s / total_d, 3), 'minutes': round(s / 60)} for d, s in sorted(dec_s.items())],
        'months': [{'month': months[i], 'minutes': round(month_s[i] / 60)} for i in range(12)],
        'top_month': months[max(range(12), key=lambda i: month_s[i])],
        'hours_of_day': [round(h / 60) for h in hour_s],
        'peak_hour': peak_hour,
        'weekdays': [round(x / 60) for x in wday_s],
        'biggest_day': {'date': big_day, 'minutes': round(day_s[big_day] / 60)},
        'streak': {'days': best, 'ended': best_end},
        'days_listened': len(day_s),
        'on_repeat': ({'date': repeat[0], 'times': day_song[repeat], 'title': meta[repeat[1]]['title'],
                       'artist': meta[repeat[1]]['track_artist'], 'id': meta[repeat[1]]['id'],
                       'album_id': meta[repeat[1]]['album_id']} if repeat and day_song[repeat] > 1 else None),
        'first_song': ({'title': meta[first['skey']]['title'], 'artist': meta[first['skey']]['track_artist'],
                        'id': meta[first['skey']]['id'], 'album_id': meta[first['skey']]['album_id'],
                        'date': datetime.datetime.fromtimestamp(first['started']).date().isoformat()} if first else None),
        'rooms': [{'room': r, 'minutes': round(s / 60)} for r, s in rooms.most_common(5)],
        'new_artists': len(new_artists),
        'persona': {'name': persona[0], 'why': persona[1]},
        'household_share': round(secs / house_secs, 3) if who and house_secs else None,
        'podcasts': pod,
        'since': datetime.datetime.fromtimestamp(plays[0]['started']).date().isoformat(),
    })
    return out


def _artist_key(name):
    return state.artist_key(name)


# ------------------------------------------------------------------ the DJ
SLIDES = ['intro', 'minutes', 'top_artist', 'top_artists', 'top_song', 'top_songs', 'genres', 'decades',
          'clock', 'persona', 'outro']

SYSTEM = """You are the DJ on MusicMate, a family's home radio station, hosting their end-of-year "Wrapped": a short, glitzy video of one listener's year in music. You speak one or two short lines per slide, over music.

Write for a teenager as much as a parent: quick, warm, funny, a bit hype but never cringe, never sarcastic about their taste. Talk to the listener by name now and then. Keep every line under 28 words; most under 18. Spoken English only: no emoji, no stage directions, no markdown. Use the numbers you are given exactly (round big ones naturally: "almost three thousand minutes"); never invent facts, chart positions or quotes. A short, true, widely known tidbit about a top artist is welcome once or twice. If the year is still going, say so lightly ("and the year's not even over").

Return JSON only: {"lines": {"<slide id>": "<what you say>", ...}} with exactly these slide ids: """ + ', '.join(SLIDES)


def _brief(st):
    keep = {k: st.get(k) for k in ('year', 'name', 'minutes', 'hours', 'plays', 'songs', 'artists', 'albums', 'top_month',
                                   'peak_hour', 'biggest_day', 'streak', 'days_listened', 'on_repeat', 'first_song',
                                   'new_artists', 'persona', 'household_share', 'podcasts', 'since')}
    keep['top_artists'] = [{k: a[k] for k in ('name', 'plays', 'minutes')} for a in st['top_artists'][:5]]
    keep['top_songs'] = [{k: s[k] for k in ('title', 'artist', 'plays')} for s in st['top_songs'][:5]]
    keep['genres'] = st['genres'][:4]
    keep['decades'] = st['decades']
    keep['year_finished'] = datetime.date.today().year > st['year']
    keep['listener_is_household'] = not st['who']
    return keep


def _lines_claude(st):
    import dj
    key = dj._key()
    if not key:
        return None
    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=1)
    r = client.messages.create(model=dj.MODEL, max_tokens=4000, output_config={'effort': 'low'}, system=SYSTEM,
                               messages=[{'role': 'user', 'content': json.dumps(_brief(st), ensure_ascii=False)}])
    text = ''.join(b.text for b in r.content if b.type == 'text')
    m = re.search(r'\{.*\}', text, re.S)
    lines = json.loads(m.group(0))['lines'] if m else {}
    return {k: re.sub(r'[*_#`]', '', str(v)).strip() for k, v in lines.items() if k in SLIDES and v}


def _lines_plain(st):
    n = st['name'] if st['who'] else 'everybody'
    a = st['top_artists'][0] if st['top_artists'] else None
    s = st['top_songs'][0] if st['top_songs'] else None
    g = st['genres'][0]['name'] if st['genres'] else None
    d = max(st['decades'], key=lambda x: x['share'])['decade'] if st['decades'] else None
    h = st['peak_hour']
    hh = '%d %s' % ((h % 12) or 12, 'in the morning' if h < 12 else 'in the afternoon' if h < 17 else 'at night')
    return {
        'intro': "Hey %s. It's your year in music, %d. Let's see what you've been up to." % (n, st['year']),
        'minutes': '%s minutes of music. That is %s hours of your life, well spent.' % (f"{st['minutes']:,}", st['hours']),
        'top_artist': ('Your number one artist this year: %s.' % a['name']) if a else 'Your top artist is coming up.',
        'top_artists': 'And here is the rest of your top five.',
        'top_song': ('Your song of the year? %s, by %s. %d plays.' % (s['title'], s['artist'], s['plays'])) if s else '',
        'top_songs': 'These are the songs you kept coming back to.',
        'genres': ('Mostly %s, with a little of everything else.' % g) if g else 'A bit of everything.',
        'decades': ('Your favourite decade was the %ds.' % d) if d else '',
        'clock': 'Your music hour? Around %s.' % hh,
        'persona': 'Which makes you... a %s. %s.' % (st['persona']['name'], st['persona']['why'].capitalize()),
        'outro': "That's your %d. Thanks for listening, %s. See you next year." % (st['year'], n),
    }


def _speak(text, path):
    """edge-tts now and then answers with no audio (NoAudioReceived) for a line
    it voices fine a moment later, so each line gets three tries"""
    import asyncio, edge_tts

    async def go():
        await edge_tts.Communicate(text, VOICE, rate='+6%').save(str(path))
    for attempt in range(4):
        try:
            asyncio.run(go())
            return True
        except Exception as e:
            print('wrapped: voice try %d: %s' % (attempt + 1, e), flush=True)
            time.sleep(1.5 * (attempt + 1))
    try:
        Path(path).unlink()                 # an empty file would pass for a voiced line
    except OSError:
        pass
    return False


def _key(st):
    """a finished year is keyed by its numbers; the current one by the day, so
    the DJ is written once a day (overnight, by prewarm) and opens instantly"""
    if st['year'] >= datetime.date.today().year:
        return '%d-%d-d%s' % (st['year'], st['who'], datetime.date.today().strftime('%Y%m%d'))
    core = {k: v for k, v in st.items() if k != 'generated'}
    return '%d-%d-%s' % (st['year'], st['who'], hashlib.sha1(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()[:10])


def snapshot(year, who=0):
    """the numbers the DJ spoke, when today's narration exists, so what is on
    screen matches what is said; otherwise the live numbers"""
    if year >= datetime.date.today().year:
        k = '%d-%d-d%s' % (year, who, datetime.date.today().strftime('%Y%m%d'))
        p = CACHE / k / 'stats.json'
        if p.exists():
            try:
                return json.loads(p.read_text(encoding='utf-8'))
            except ValueError:
                pass
    return stats(year, who)


def narration(year, who=0):
    """{'state': 'ready', 'lines': {slide: {'text', 'url'}}} or {'state': 'running'}; starts the job if needed"""
    st = snapshot(year, who)
    if st['empty']:
        return {'state': 'empty'}
    k = _key(st)
    d = CACHE / k
    done = d / 'lines.json'
    if done.exists():
        lines = json.loads(done.read_text(encoding='utf-8'))
        # a line the voice service would not speak keeps its text, for the caption
        return {'state': 'ready', 'key': k, 'lines': {s: {'text': t, 'url': ('/api/wrapped/voice/%s/%s.mp3' % (k, s))
                                                           if _voiced(d / (s + '.mp3')) else None}
                                                       for s, t in lines.items()}}
    with _lock:
        job = _jobs.get(k)
        if job and job['state'] == 'running':
            return {'state': 'running'}
        if job and job['state'] == 'error' and time.time() - job['at'] < 120:
            return {'state': 'error', 'error': job['error']}
        _jobs[k] = {'state': 'running', 'at': time.time()}
    threading.Thread(target=_make, args=(k, st), daemon=True).start()
    return {'state': 'running'}


def _make(k, st):
    d = CACHE / k
    try:
        lines = None
        try:
            lines = _lines_claude(st)
        except Exception as e:
            print('wrapped: claude failed:', type(e).__name__, str(e)[:200], flush=True)
        plain = _lines_plain(st)
        lines = {s: (lines or {}).get(s) or plain.get(s) for s in SLIDES}
        lines = {s: t for s, t in lines.items() if t}
        d.mkdir(parents=True, exist_ok=True)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(4) as ex:      # eleven lines one after another took two minutes
            spoken = sum(ex.map(lambda st_: _speak(st_[1], d / (st_[0] + '.mp3')), lines.items()))
        if not spoken:
            raise RuntimeError('the voice service did not answer')
        (d / 'stats.json').write_text(json.dumps(st, ensure_ascii=False, default=str), encoding='utf-8')
        (d / 'lines.json').write_text(json.dumps(lines, ensure_ascii=False, indent=1), encoding='utf-8')
        _jobs[k] = {'state': 'ready', 'at': time.time()}
        # older narrations of the same year and person are superseded
        for old in CACHE.glob('%d-%d-*' % (st['year'], st['who'])):
            if old.name != k:
                for f in old.iterdir():
                    f.unlink()
                old.rmdir()
    except Exception as e:
        print('wrapped: narration failed:', type(e).__name__, str(e)[:200], flush=True)
        _jobs[k] = {'state': 'error', 'error': str(e)[:200], 'at': time.time()}


def _voiced(p):
    try:
        return p.stat().st_size > 1000
    except OSError:
        return False


def voice_path(k, slide):
    if not re.match(r'^\d{4}-\d+-(?:[0-9a-f]{10}|d\d{8})$', k) or slide not in SLIDES:
        return None
    p = CACHE / k / (slide + '.mp3')
    return p if _voiced(p) else None


def prewarm():
    """this year's narration for the household and everyone who listened, made
    once a day in the small hours so pressing Wrapped never waits"""
    y = datetime.date.today().year
    import podcasts as _p
    for who in [0] + [p['id'] for p in _p.people()]:
        if y not in years(who):
            continue
        r = narration(y, who)
        while r.get('state') == 'running':
            time.sleep(5)
            r = narration(y, who)


def start():
    def loop():
        time.sleep(120)
        while True:
            try:
                prewarm()
            except Exception as e:
                print('wrapped prewarm:', e, flush=True)
            now = datetime.datetime.now()
            nxt = (now + datetime.timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)
            time.sleep(max(60, (nxt - now).total_seconds()))
    threading.Thread(target=loop, daemon=True, name='wrapped').start()
