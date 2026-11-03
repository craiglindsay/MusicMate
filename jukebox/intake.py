"""Auto-intake: new albums from the download pipeline into the library, unattended.

Until 30 Oct 2026 every step below was run by hand from a Claude session. The
server now runs them in a thread every ten minutes (JUKEBOX_INTAKE_MIN):

  1. convert  UNPoller's batch_convert.py, when Lidarr's Music folder holds
              lossless files (FLAC -> AAC 256, the FLAC deleted)
  2. release  UNPoller's release_completed.py, when that folder holds audio:
              complete, converted albums untouched for 15 minutes are moved
              into staging (a same-drive rename, so never seen half-copied),
              unmonitored in Lidarr and written to its ledger
  3. ingest   library/ingest.py --apply, when staging differs from what the
              last pass left there: tags, art, dedupe, moved into D:\\Music
              (Christmas, Compilations and Soundtracks are on C: behind
              junctions) and registered in library.db
  4. rebuild  etl.py, when ingest filed anything. Ids are stable across
              rebuilds (idmap.py), so nothing playing is touched; open pages
              notice the new build and say "New music added".

Steps 1-2 are skipped while UNPoller's pipeline_driver.py is running, because
it does them itself, and when the pipeline folder (JUKEBOX_PIPELINE) does not
exist. A pass is deferred while a rescan or a karaoke separation runs: one
CPU-heavy job at a time.

What ingest will not file stays in staging and is listed under "Needs
attention" on the System page, with the reason from ingestplan.json: AcoustID
disagreeing with the folder, not enough space on the album's drive, files left
behind. From there a person can file it anyway (ingest --force) or set it
aside into ToBeProcessed_holding, which takes it out of the loop's view.

Pause the loop by creating jukebox/logs/INTAKE_OFF; delete it to resume.
"""
import os, sys, json, time, shutil, ctypes, threading, subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB_DIR = HERE.parent / 'library'
PLAN = LIB_DIR / 'ingestplan.json'
PIPELINE = Path(os.environ.get('JUKEBOX_PIPELINE') or r'D:\Projects\UNPoller\music-pipeline')
STAGING = Path(os.environ.get('MUSICMATE_STAGING') or (Path.home() / 'ToBeProcessed'))
ASIDE = Path.home() / 'ToBeProcessed_holding' / 'needs_attention'
LIDARR_MUSIC = Path.home() / 'Music'          # Lidarr's root folder
OFF = HERE / 'logs' / 'INTAKE_OFF'
LOGFILE = HERE / 'logs' / 'intake.log'
INTERVAL = int(os.environ.get('JUKEBOX_INTAKE_MIN') or 10) * 60
LOSSLESS = {'.flac', '.ape', '.wv', '.wav', '.aiff', '.aif'}
AUDIO = LOSSLESS | {'.m4a', '.mp3', '.ogg', '.opus', '.aac', '.wma'}

STATE = {'state': 'idle', 'step': '', 'last': None, 'next': None, 'error': None,
         'added': [], 'attention': [], 'log': [], 'lock': threading.Lock()}
_wake = threading.Event()
ON_ADDED = []               # called after a rebuild that brought new albums in (wanted.py's refresh)
_left = {'sig': None}       # staging as the last ingest left it


def log(line):
    line = time.strftime('%m-%d %H:%M:%S ') + line
    with STATE['lock']:
        STATE['log'].append(line)
        del STATE['log'][:-400]
    try:
        LOGFILE.parent.mkdir(exist_ok=True)
        if LOGFILE.exists() and LOGFILE.stat().st_size > 2_000_000:
            LOGFILE.replace(LOGFILE.with_suffix('.log.1'))
        with open(LOGFILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def _set(**kw):
    with STATE['lock']:
        STATE.update(kw)


def running():
    return STATE['state'] == 'running'


def run(step, cmd, cwd):
    """Run one script, its output into the log. Returns the exit code."""
    _set(step=step)
    log('> ' + ' '.join(str(c) for c in cmd))
    p = subprocess.Popen([str(c) for c in cmd], cwd=str(cwd), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, errors='replace', bufsize=1,
                         env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'},
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for line in iter(p.stdout.readline, ''):
        if line.strip():
            log('  ' + line.rstrip())
    p.stdout.close()
    rc = p.wait()
    if rc:
        log(f'  ({step} exited {rc})')
    return rc


# ------------------------------------------------------------------ what is there
def _pid_alive(pid):
    # never os.kill(pid, 0) on Windows: that TERMINATES the process
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def driver_running():
    """UNPoller's pipeline_driver.py converts and releases on its own while it
    runs. Its pid file outlives it, and a pid can be reused, so it also has to
    have logged in the last two hours."""
    try:
        pid = int((PIPELINE / 'pipeline_driver.pid').read_text().strip())
        fresh = time.time() - (PIPELINE / 'pipeline_driver.log').stat().st_mtime < 2 * 3600
        return fresh and _pid_alive(pid)
    except (OSError, ValueError):
        return False


def _files(root, exts):
    for dp, _, fs in os.walk(root):
        for f in fs:
            if os.path.splitext(f)[1].lower() in exts:
                yield os.path.join(dp, f)


def staging_albums():
    out = []
    try:
        for a in sorted(os.listdir(STAGING)):
            ap = STAGING / a
            if ap.is_dir():
                out += [(a, b) for b in sorted(os.listdir(ap)) if (ap / b).is_dir()]
    except OSError:
        pass
    return out


def signature():
    """Staging as a comparable value: every album folder with its file count
    and newest write. Unchanged means ingest has nothing new to look at."""
    sig = []
    for a, b in staging_albums():
        n, newest = 0, 0
        for dp, _, fs in os.walk(STAGING / a / b):
            for f in fs:
                n += 1
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(dp, f)))
                except OSError:
                    pass
        sig.append((a, b, n, int(newest)))
    return tuple(sig)


def _plan():
    try:
        return json.loads(PLAN.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []


def attention():
    """Albums still in staging and why ingest left them there."""
    plan = {(p['artist'], p['album']): p for p in _plan()}
    out = []
    for a, b in staging_albums():
        p = plan.get((a, b)) or {}
        o = p.get('outcome') or ''
        ac = p.get('acoustid') or {}
        if o == 'blocked':
            why = (f"AcoustID names another artist for {ac.get('disagree')} of the "
                   f"{(ac.get('agree') or 0) + (ac.get('disagree') or 0)} tracks it recognised")
        elif o.startswith('held'):
            why = 'not enough space on the drive it would go to'
        elif o.startswith('partial'):
            why = 'filed in part; ' + o.split(': ', 1)[-1]
        elif not p:
            why = 'not looked at yet' if running() else 'no audio files ingest could read'
        else:
            why = o or 'left in staging'
        out.append({'artist': a, 'album': b, 'why': why, 'outcome': o, 'dest': p.get('dest'),
                    'pool': p.get('pool'), 'tracks': p.get('tracks'),
                    'dupes': p.get('dupes_same_album') or 0})
    return out


# ------------------------------------------------------------------ one pass
def _ingest(args, label):
    """ingest.py with args; returns the albums it filed."""
    before = time.time()
    rc = run(label, [sys.executable, 'ingest.py', '--apply'] + args, LIB_DIR)
    if rc == 3:
        log('  ingest.py is already running elsewhere; next pass')
        return []
    if PLAN.exists() and PLAN.stat().st_mtime < before:
        return []
    return [p for p in _plan() if (p.get('outcome') or '').startswith(('ingested', 'partial'))]


def _rebuild(added):
    if not added:
        return
    rc = run('rebuild', [sys.executable, 'etl.py'], HERE)
    if rc:
        raise RuntimeError('etl.py failed; the new albums are filed but not yet visible')
    # a new artist's original release years (MusicBrainz), patched into the read model
    if run('original years', [sys.executable, 'origyear.py'], HERE):
        log('origyear.py failed; new albums keep their tagged years until the next run')
    now = time.time()
    new = [{'artist': p.get('albumartist') or p['artist'], 'album': p.get('dest', '').split('/')[-1],
            'pool': p.get('pool'), 'dest': p.get('dest'), 'tracks': p.get('tracks'), 'at': now}
           for p in added]
    with STATE['lock']:
        STATE['added'] = (new + STATE['added'])[:40]
    log(f'added {len(new)} album(s): ' + '; '.join(f"{n['artist']} - {n['album']}" for n in new))
    for fn in ON_ADDED:
        try:
            fn()
        except Exception as e:
            log(f'after adding: {e}')


def cycle(busy, force=False):
    if OFF.exists():
        _set(state='off', step='')
        return
    why = busy()
    if why:
        log(f'deferred: {why}')
        return
    with STATE['lock']:
        if STATE['state'] == 'running':
            return
        STATE.update(state='running', step='looking', error=None)
    try:
        if PIPELINE.is_dir() and not driver_running():
            if next(_files(LIDARR_MUSIC, LOSSLESS), None):
                run('convert', [sys.executable, 'batch_convert.py', str(LIDARR_MUSIC), '--workers', '4'], PIPELINE)
            if next(_files(LIDARR_MUSIC, AUDIO), None):
                run('release', [sys.executable, 'release_completed.py'], PIPELINE)
        sig = signature()
        if sig and (force or sig != _left['sig']):
            added = _ingest([], 'ingest')
            _left['sig'] = signature()
            _rebuild(added)
        elif not sig:
            _left['sig'] = sig
    except Exception as e:
        log('ERROR ' + str(e))
        _set(error=str(e))
    finally:
        _set(state='idle', step='', last=time.time())
        _set(attention=attention())         # after: it reads the state


def act(kind, artist, album):
    """Needs attention: 'file' ingests one held album with --force, 'aside'
    moves it out of staging. Runs in the caller's thread."""
    with STATE['lock']:
        if STATE['state'] == 'running':
            raise RuntimeError('intake is busy; try again in a minute')
        STATE.update(state='running', step=kind, error=None)
    try:
        if kind == 'file':
            log(f'filing {artist} / {album} as asked, despite: ' +
                next((x['why'] for x in STATE['attention'] if (x['artist'], x['album']) == (artist, album)), '?'))
            _rebuild(_ingest(['--artist', artist, '--album', album, '--force'], 'ingest (asked)'))
            _left['sig'] = signature()
        elif kind == 'aside':
            dst = ASIDE / artist / album
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(STAGING / artist / album), str(dst))
            try:
                (STAGING / artist).rmdir()          # only if now empty
            except OSError:
                pass
            log(f'set aside {artist} / {album} -> {dst}')
    except Exception as e:
        log('ERROR ' + str(e))
        _set(error=str(e))
        raise
    finally:
        _set(state='idle', step='', last=time.time())
        _set(attention=attention())         # after: it reads the state


def kick():
    """Run a pass now rather than at the next interval."""
    _left['sig'] = None
    _wake.set()


def loop(busy):
    time.sleep(90)                      # let the server settle after a start
    while True:
        try:
            cycle(busy)
        except Exception as e:
            log('ERROR ' + str(e))
        _set(next=time.time() + INTERVAL)
        _wake.wait(INTERVAL)
        _wake.clear()


def start(busy):
    _set(attention=attention())
    threading.Thread(target=loop, args=(busy,), daemon=True, name='intake').start()


def status():
    with STATE['lock']:
        d = {k: v for k, v in STATE.items() if k != 'lock'}
    d['log'] = d['log'][-80:]
    d['off'] = OFF.exists()
    d['interval'] = INTERVAL
    d['staging'] = str(STAGING)
    d['pipeline'] = PIPELINE.is_dir()
    return d
