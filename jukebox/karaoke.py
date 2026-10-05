"""Vocal separation for karaoke: the store, and the one-at-a-time job queue.

Separating a song costs real CPU time -- measured on this machine, 2 min 35 s
for a 4:11 track, running about 1.6x faster than realtime on four cores with no
CUDA. That is short enough to wait for once and far too long to repeat, so the
result is kept forever and the second play is instant.

Three decisions worth stating, because each one is a trap avoided:

STORED BY skey, NOT BY TRACK ID. Track ids are reassigned by every rebuild --
only about one in seven survives -- which is what once made the app play the
wrong song entirely. skey is derived from artist, title and duration, so a
rebuild leaves the stems still attached to the right music.

STORED OUTSIDE THE MUSIC LIBRARY. Nothing is ever written next to the audio.
The library stays exactly as it is, and the whole store can be deleted without
touching a single song.

ONE JOB AT A TIME. The work is CPU bound and saturates every core; running two
would make both slower and leave the server unable to serve audio. Requests
queue instead.
"""
import os, re, json, time, queue, shutil, hashlib, threading, subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
# A shadow folder beside the library, never inside it: the library scripts
# scan D:\Music for audio and would take a stem for a stray track. Folders are
# named "Artist - Title [skey]" so a person can find a song in Explorer, but
# the store only ever looks at the bracketed key. (JUKEBOX_STEMS overrides.)
_DEFAULT = Path('D:/MusicStems') if Path('D:/').exists() else Path.home() / '.jukebox' / 'stems'
STORE = Path(os.environ.get('JUKEBOX_STEMS') or _DEFAULT)
STORE.mkdir(parents=True, exist_ok=True)

# the venv the separator lives in; it is deliberately not the server's own
# interpreter, so a 3 GB machine-learning stack cannot disturb the app
VENV = Path(os.environ.get('JUKEBOX_DEMUCS_PY') or
            (HERE.parent / 'karaoke-venv' / 'Scripts' / 'python.exe'))
MODEL = 'htdemucs'
BACKING_KBPS = '128k'
VOCALS_KBPS = '96k'

_q = queue.Queue()
_state = {}                  # skey -> dict(state, pct, msg, started, title)
_lock = threading.Lock()
_worker = None

# THE QUEUE SURVIVES A RESTART. Thirty songs queued from the sofa is an hour of
# work; a server restart for an unrelated change used to throw the whole list
# away (2 Sep 2026). Every job that is still queued or running is mirrored to a
# small JSON file beside the stems, and restore() re-queues them at startup --
# a job that was mid-separation starts over, since a half-made stem is deleted
# rather than trusted. Lives in the stem store, not the repo, for the same
# reason the stems do.
QFILE = STORE / '_queue.json'
_src = {}                    # skey -> source path, kept only to persist the queue
ALIGN_ONLY = 'align:'        # a queue entry's "source" when only word alignment is wanted


def _persist():
    """Caller holds _lock."""
    try:
        live = [{'skey': k, 'title': v.get('title', ''), 'src': _src.get(k, ''),
                 'queued_at': v.get('queued_at')}
                for k, v in _state.items() if v.get('state') in ('queued', 'running')]
        live.sort(key=lambda r: r['queued_at'] or 0)
        tmp = QFILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(live, indent=0), encoding='utf-8')
        os.replace(tmp, QFILE)
    except Exception:
        pass                  # persistence is a convenience; never fail a job for it


def restore():
    """Re-queue whatever the last process left unfinished. Call once at startup."""
    try:
        jobs = json.loads(QFILE.read_text(encoding='utf-8')) if QFILE.exists() else []
    except Exception:
        return 0
    n = 0
    for j in jobs:
        skey, src = j.get('skey'), j.get('src')
        if not skey or not src:
            continue
        if src == ALIGN_ONLY:
            if ready(skey) and not aligned(skey):
                enqueue_align(skey, j.get('title', '')); n += 1
            continue
        if ready(skey) or not Path(src).exists():
            continue
        enqueue(skey, src, j.get('title', ''), queued_at=j.get('queued_at'))
        n += 1
    return n


def available():
    """Whether separation can run at all here."""
    return VENV.exists() and shutil.which('ffmpeg') is not None


_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def safe_name(title):
    t = _BAD.sub('', title or '').strip(' .')
    return t[:120] if t else ''


def dir_for(skey, title=None):
    """The folder for a song. Finds an existing one by its bracketed key
    (or the older bare-key form); otherwise names a new one from the title."""
    skey = str(skey)
    for d in STORE.glob('*[[]%s[]]' % skey):
        if d.is_dir():
            return d
    bare = STORE / skey
    if bare.is_dir():
        return bare
    name = safe_name(title)
    return STORE / ('%s [%s]' % (name, skey) if name else skey)


def ready_made():
    """skey -> when it was prepared (epoch), for every finished song."""
    out = {}
    for d in STORE.glob('*'):
        if not (d.is_dir() and not d.name.endswith('.tmp') and (d / 'env.json').exists()):
            continue
        m = re.search(r'\[([0-9a-f]{16})\]$', d.name)
        k = m.group(1) if m else d.name
        try:
            out[k] = float(json.loads((d / 'meta.json').read_text(encoding='utf-8')).get('made') or 0)
        except Exception:
            out[k] = (d / 'env.json').stat().st_mtime
    return out


def ready_keys():
    """Every skey with a finished separation, one directory scan. Used to
    mark a whole track list, where a per-row check would be a stat storm."""
    out = set()
    for d in STORE.glob('*'):
        if d.is_dir() and not d.name.endswith('.tmp') and (d / 'env.json').exists():
            m = re.search(r'\[([0-9a-f]{16})\]$', d.name)
            out.add(m.group(1) if m else d.name)
    return out


def ready(skey):
    d = dir_for(skey)
    return (d / 'backing.opus').exists() and (d / 'vocals.opus').exists() and (d / 'env.json').exists()


ALIGN_VERSION = 3               # align.py's current output; older words.json is re-aligned


def aligned(skey):
    """Has word timings from the current aligner. A version-1 file still
    serves the screen until its replacement is written, but counts as missing,
    so /api/admin/align-missing re-queues it."""
    p = dir_for(skey) / 'words.json'
    if not p.exists():
        return False
    try:
        with open(p, 'rb') as f:
            m = re.search(rb'"version":\s*(\d+)', f.read(200))
        return bool(m) and int(m.group(1)) >= ALIGN_VERSION
    except OSError:
        return False


def align_stem(skey, title=''):
    """Word timings by forced alignment (align.py, in the venv) into words.json.
    Runs after separation and on demand for songs split before it existed. A
    failure leaves the song playable: the screen falls back to its envelope
    estimate when there is no words.json."""
    d = dir_for(skey, title)
    rc, last = _run([str(VENV), str(HERE / 'align.py'), '--dir', str(d)])
    if rc != 0:
        raise RuntimeError('alignment failed: %s' % last[:200])
    return (d / 'words.json').exists()


def enqueue_align(skey, title=''):
    """Queue word alignment for a song that is split but has no words.json."""
    if not ready(skey) or aligned(skey):
        return status(skey)
    with _lock:
        if skey in _state and _state[skey]['state'] in ('queued', 'running'):
            return dict(_state[skey])
        _state[skey] = {'state': 'queued', 'pct': 0, 'msg': 'waiting to align', 'title': title,
                        'queued_at': time.time()}
        _src[skey] = ALIGN_ONLY
        _persist()
    _q.put((skey, ALIGN_ONLY, title))
    _ensure_worker()
    return status(skey)


def status(skey):
    """What the screen needs to decide which microphone to draw."""
    if ready(skey):
        return {'state': 'ready', 'pct': 100}
    with _lock:
        s = _state.get(skey)
    if s:
        return dict(s)
    return {'state': 'none', 'pct': 0}


def _set(skey, **kw):
    with _lock:
        s = _state.setdefault(skey, {'state': 'queued', 'pct': 0, 'msg': ''})
        s.update(kw)
        if 'state' in kw:
            _persist()


def enqueue(skey, src, title='', queued_at=None):
    """Ask for a song to be separated. Idempotent: asking twice does nothing."""
    if ready(skey):
        return status(skey)
    with _lock:
        if skey in _state and _state[skey]['state'] in ('queued', 'running'):
            return dict(_state[skey])
        _state[skey] = {'state': 'queued', 'pct': 0, 'msg': 'waiting', 'title': title,
                        'queued_at': queued_at or time.time()}
        _src[skey] = str(src)
        _persist()
    _q.put((skey, str(src), title))
    _ensure_worker()
    return status(skey)


def cancel(skey):
    """Drop a job that has not started. A running job is left alone -- killing
    it halfway wastes the minutes already spent and leaves debris."""
    with _lock:
        s = _state.get(skey)
        if s and s['state'] == 'queued':
            s['state'] = 'cancelled'
            _persist()
            return True
    return False


def _run(cmd, on_pct=None):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True, errors='replace', bufsize=1)
    last = ''
    for line in iter(p.stdout.readline, ''):
        last = line.strip() or last
        if on_pct:
            m = re.findall(r'(\d+)%\|', line)
            if m:
                on_pct(int(m[-1]))
    p.stdout.close()
    return p.wait(), last


def _separate(skey, src, title):
    d = dir_for(skey, title)
    tmp = d.with_name(d.name + '.tmp')
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        _set(skey, state='running', pct=0, msg='separating', started=time.time())
        rc, last = _run([str(VENV), '-m', 'demucs', '--two-stems=vocals',
                         '-n', MODEL, '-o', str(tmp), src],
                        on_pct=lambda p: _set(skey, pct=int(p * 0.88)))
        if rc != 0:
            raise RuntimeError('separation failed: %s' % last[:200])

        outs = list(tmp.glob('*/*/no_vocals.wav'))
        if not outs:
            raise RuntimeError('separation produced nothing')
        wav_b = outs[0]
        wav_v = wav_b.with_name('vocals.wav')

        # The envelope is read from the LOSSLESS vocal, before encoding, so the
        # numbers describe the voice rather than the codec.
        _set(skey, pct=90, msg='analysing')
        import envelope as env
        data = env.analyse(wav_v)

        _set(skey, pct=94, msg='encoding')
        for wav, out, kbps in ((wav_b, tmp / 'backing.opus', BACKING_KBPS),
                               (wav_v, tmp / 'vocals.opus', VOCALS_KBPS)):
            rc, last = _run(['ffmpeg', '-v', 'error', '-y', '-i', str(wav),
                             '-c:a', 'libopus', '-b:a', kbps, str(out)])
            if rc != 0:
                raise RuntimeError('encode failed: %s' % last[:200])

        (tmp / 'env.json').write_text(json.dumps(data, separators=(',', ':')), encoding='utf-8')
        (tmp / 'meta.json').write_text(json.dumps(
            {'skey': skey, 'title': title, 'source': str(src), 'model': MODEL,
             'made': time.time(), 'seconds': round(time.time() - _state[skey]['started'], 1)},
            indent=1), encoding='utf-8')
        for w in tmp.glob('*/*/*.wav'):
            w.unlink()
        for sub in sorted(tmp.glob('*/*'), reverse=True):
            if sub.is_dir():
                shutil.rmtree(sub, ignore_errors=True)
        for sub in tmp.glob('*'):
            if sub.is_dir():
                shutil.rmtree(sub, ignore_errors=True)

        # move into place only once everything succeeded, so a half-finished
        # job can never look ready
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        tmp.rename(d)
        # the song is singable from here; word alignment is a refinement
        _set(skey, state='running', pct=97, msg='aligning words')
        try:
            align_stem(skey, title)
        except Exception as e:
            print('karaoke: alignment skipped for %s: %s' % (skey, str(e)[:120]))
        _set(skey, state='ready', pct=100, msg='')
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        _set(skey, state='error', pct=0, msg=str(e)[:300])


def _loop():
    while True:
        skey, src, title = _q.get()
        try:
            with _lock:
                s = _state.get(skey)
                if s and s['state'] == 'cancelled':
                    continue
            if src == ALIGN_ONLY:
                if ready(skey) and not aligned(skey):
                    _set(skey, state='running', pct=10, msg='aligning words', started=time.time())
                    try:
                        align_stem(skey, title)
                    except Exception as e:
                        # a song without timed lyrics cannot be aligned; that is not
                        # a failure of the song, which stays singable as before
                        print('karaoke: alignment skipped for %s: %s' % (skey, str(e)[:120]))
                _set(skey, state='ready', pct=100, msg='')
            elif not ready(skey):
                _separate(skey, src, title)
        except Exception as e:                      # never let the worker die
            _set(skey, state='error', msg=str(e)[:300])
        finally:
            _q.task_done()


def _ensure_worker():
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_loop, daemon=True, name='karaoke')
        _worker.start()


def queued():
    """Everything the queue currently knows about, newest first."""
    with _lock:
        out = [dict(v, skey=k) for k, v in _state.items()]
    out.sort(key=lambda r: -(r.get('queued_at') or 0))
    return out


def store_stats():
    n = size = 0
    for d in STORE.glob('*'):
        if d.is_dir() and (d / 'env.json').exists():
            n += 1
            for f in d.glob('*'):
                size += f.stat().st_size
    return {'songs': n, 'bytes': size, 'path': str(STORE)}
