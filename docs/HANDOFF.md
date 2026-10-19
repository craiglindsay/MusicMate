# Jukebox handoff — 1 September 2026

Working notes for picking this up again. Written for whoever continues the work,
including a future session with no memory of today.

---

## Start here

```
cd <scratchpad>/jukebox
python server.py --port 8730
```

**The running server on 8730 predates today's karaoke work.** `server.py` is read
once at startup, so the karaoke endpoints are NOT live until it is restarted.
That restart is the first thing to do.

The UI (`ui.html`) is read fresh on every request, so UI changes need no restart.

---

## What was finished today

### 1. Chrome 74 fallbacks for the television (done, deployed)

`/diag` on the TV proved it runs **Chrome 74 (2019)**, 1920x1080. It lacks CSS
`inset`, `clamp()`, `min()`, `max()`, `aspect-ratio`, `content-visibility`,
`backdrop-filter`, `:has()`, and `loading="lazy"`. It DOES have `zoom`, flex
`gap`, `@supports`, IntersectionObserver and all ES2019 syntax.

A declaration Chrome 74 cannot parse is **discarded entirely**, which broke
things silently rather than degrading them:

| Missing | Symptom |
|---|---|
| `clamp()` | whole `flex` shorthand thrown away, artwork rendered at natural size |
| `inset` | fixed overlays had no offsets, so the output picker opened invisibly |
| `aspect-ratio` | square tiles collapsed |
| `loading="lazy"` | 2,500 covers all fetched at once — the real cause of TV slowness |

Fixes, all in `ui.html`: `@supports not (...)` blocks near the end of the
stylesheet, plus a `lazySrc()` + IntersectionObserver shim. Confirmed fixed by
the user: stretched artist oval and cropped album art.

**Never use `?.`, `??` or `replaceAll` in `ui.html`** — they are parse errors on
that browser and would kill the entire script.

### 2. WebGL verdict made re-testable (done, needs a TV reload to take effect)

`glFallback()` wrote `jb.gltier = 9` to localStorage and never retried WebGL
again. That verdict was almost certainly reached while the page was drowning in
un-lazy-loaded artwork — it measured whole-page frame time, not the shader. A
`GLPROBE` stamp now gives every device one fair retest; a genuinely weak GPU
still demotes itself within 90 frames.

### 3. Karaoke (implemented for web and phone, untested by a human)

**Server**

- `envelope.py` — offline analysis of a vocal stem. 20 Hz RMS, word onsets,
  held notes, climax window. **3 seconds, ~15 KB per song.** Standalone CLI too.
- `karaoke.py` — stem store and a single-job queue.
- Endpoints in `server.py`: `/api/karaoke/status/{tid}`,
  `POST|DELETE /api/karaoke/prepare/{tid}`, `/api/karaoke/queue`,
  `/api/karaoke/{tid}/backing.opus`, `/vocals.opus`, `/envelope`.

**Three decisions, each avoiding a trap already hit in this project**

- **Keyed by `skey`, never track id.** Only ~1 in 7 track ids survives a rebuild;
  that is what once made the app play the wrong song entirely.
- **Stored outside the music library**, in the shadow folder `D:\MusicStems`
  (since 1 Sep 2026; was `~/.jukebox/stems`). One folder per song named
  `Artist - Title [skey]`; the store finds a song by the bracketed key only,
  and still accepts a bare-key folder. Nothing is written next to the audio;
  the store can be deleted without touching a song.
- **One job at a time.** The work saturates every core.

**Client** — mic button on Now Playing (`#npmic`, beside the heart), hidden when
separation is unavailable, dotted when already prepared, refuses politely when a
song has no synced lyrics. Track rows carry the same mic (`.trmic`) with the
same dot. Karaoke screen is `#kar`.

**Karaoke page (1 Sep 2026)** — nav item `#/karaoke`, view `vKaraoke`, data
from `/api/karaoke/library` (cache-only): `ready` (split songs, grouped by
artist, a row opens the karaoke screen), `inflight` (the queue, polled every
4 s while the page is open, page rebuilds when it drains), `candidates` (songs
with time-synced lyrics not yet split). A stem is keyed by skey, so
`_tracks_for_skeys` picks one library track per key, preferring a studio copy.

**Timing (1 Sep 2026)** — two things keep the voice and the words on the beat:

- The vocal stem follows the backing stem by a playback-rate nudge (±3 %,
  proportional to the error), with a hard seek only past 250 ms. Two media
  elements never start together, and the old guard tolerated 80 ms, which is
  audible as the voice arriving early.
- A Music on/off switch (`#karmus`, `karSetMusic`) beside the vocals switch,
  a ramp on the backing gain. Music off with the voice off would be silence,
  so turning the music off flips the voice on.
- Line ends come from the vocal envelope (`karLineEnd`): a line lasts until
  the first 0.7 s of silence in the voice after it has been going, capped at
  the next line. The old word-count estimate gave "do-do-do-do-do-do-do-do-do"
  3 s when it is sung for 8; that is now the fallback only. Ends are re-read
  when the offset changes, since the scan runs in audio time.
- Play/pause (`#karplay`, space bar). Karaoke owns the keyboard while open:
  space once started the app's own player under the stems.
- A lyric offset (`KAR.offset`, seconds; positive = words later) with −/+
  buttons in the footer (`#karsync`, 100 ms a press, hold to run, tap the readout to reset; ±60 s). Only the lyric clock uses
  it; the envelope and visuals stay on audio time because they were measured
  from it. Saved per `skey` in `state.db` table `kar_sync` via
  `POST /api/karaoke/offset/{tid}`, returned in `/api/karaoke/status`.

**Measured on this machine** (4-core i7-8559U, no CUDA):

| | |
|---|---|
| Separation | **2 min 35 s for a 4:11 track** (~1.6x faster than realtime) |
| Full job incl. encode + analysis | ~3 min 11 s |
| Stems as Opus | 3.7 MB backing + 2.7 MB vocals |

Two songs are already prepared: The Beatles "I Need You" and
The Rolling Stones "Shattered".

---

## Dependencies

`demucs` and `torch` live in an **isolated venv** so a 3 GB ML stack cannot
disturb the app: `<scratchpad>/karaoke-venv` (~3 GB). `karaoke.py` finds it via
`JUKEBOX_DEMUCS_PY`, defaulting to that path. `ffmpeg` must be on PATH (it is).
`karaoke.available()` reports whether both are present.

The stem store path is overridable with `JUKEBOX_STEMS` (default `D:\MusicStems`, or `~/.jukebox/stems` when there is no D: drive).

---

## Debug affordances added today (all harmless, all worth keeping)

| URL / tool | What it does |
|---|---|
| `/diag` | browser capability report, ES5-only so it runs anywhere |
| `/?diag=1` | posts computed geometry of key elements to `/api/diag` |
| `GET /api/diag` | last 5 submissions |
| `/?kar=<tid>` | opens the karaoke screen directly |
| `/?np=<tid>` | plays that track and opens Now Playing (needs `--autoplay-policy=no-user-gesture-required` headless) |
| `/?kar=<tid>&at=<sec>` | ...and seeks, for checking mid-song states |
| `/?bd=<style>` | forces a backdrop style for this load only (any `BD_STYLES` id, including `cycle`); `&karbd=same` puts it behind the karaoke lyrics too |
| `window.onerror` trap | surfaces script errors into the page (mockup only) |

**Headless rendering is the highest-value tool of the session.** Three layout
bugs were shipped to the user before this was set up, and every one after was
caught before they saw it:

```
"/c/Program Files/Google/Chrome/Application/chrome.exe" --headless=new \
  --disable-gpu --autoplay-policy=no-user-gesture-required \
  --window-size=1280,800 --virtual-time-budget=7000 \
  --screenshot=out.png "http://127.0.0.1:8730/?kar=430&at=11"
```

Note: `--dump-dom` returns almost immediately and will catch the page in its
first moments — it misled a diagnosis today. `--screenshot` honours the virtual
time budget; prefer it.

---

## Wrong covers and album artists (1 Sep 2026)

Two albums showed another record's sleeve. Both traced to the same shape of
error: the art fetcher (`library/artfetch.py`) matched on the album title and
was too generous about the artist. "A View To A Kill" carried a bad
`albumartist` tag (Shādz of Lingo on one file, John Barry on the other), so
Deezer returned the rap album of that name; "Electronic" accepted "Electronic
dystopia" because the returned title merely started with ours.

- `artfetch.artist_ok` now requires the same performer: a fuzzy match, a
  shared credit token ("B.B. King/Robert Cray" vs "B.B. King"), or an
  extension that reads as a band credit ("& The Wailers", "The … Group").
  "Eagles in Drag", "Queen Marion", "Enya Lisa" no longer pass.
- A title that extends ours is rejected when the extra words name another
  record (`WRONG_RECORD`: greatest hits, live, remix, cast recording, …).
- `library/fixart.py <folder>` replaces one cover from MusicBrainz + the
  Cover Art Archive (artist-credit matched), updates `artcache.db`, and with
  `--embed` rewrites the picture inside the files. 27 covers were replaced
  this way after auditing every existing hit against the new rules; "She
  Loves You" had no archive cover and fell back to its embedded art.
- The two Duran Duran files were retagged (`albumartist` = Duran Duran,
  logged in `tag_backup` as op `fix-albumartist`) and had the right sleeve
  embedded. A rebuild followed.

The album-artist audit (1 Sep 2026, approved by the user): 106 albums whose
album-artist tag named nobody on the record. 16 were another act, a genre
or noise (Madonna as Louisiana Red, Moby as Ellis Beggs & Howard, "Christmas
Music", "Soundtrack", Buddha Bar garbage) and were retagged; 39 were one act
under two spellings (OMD, The B-52's, DJ Tiësto, Public Image Ltd., ...) and
had album and track tags unified; 76 files carried a literal `&amp;` in a
tag and were fixed. 2,702 field changes, all in `tag_backup` as ops
`audit-albumartist`, `audit-unify`, `audit-entity`. Covers for the retagged
albums were refetched from the Cover Art Archive where it had them. The 45
still flagged are collaboration credits ("Herb Alpert & Tijuana Brass") and
compilations, kept on purpose. Open question for the user: whether such
credits should browse under the main artist.

## One library (1 Sep 2026)

A second top-level folder had been a staging pool for purchased music that
had not been organised; the user's intent is that VariousArtists holds every artist with
complete albums and the other top-level folders hold collections. Done today:

- A one-shot fold script (since removed; its `art_follow` helper now lives in
  `musiclib.py`) moved all 7,564 staged files to the same Artist/Album path
  under VariousArtists (op `fold`), renaming the artist
  folders VariousArtists spelled worse ("Doors" -> "The Doors", "OMD" -> the
  full name), filing the two loose Duran Duran folders, and putting stray
  single files into "<Artist>/Assorted". The staging folder is gone.
- `library/resolve_pairs.py` settled the 144 untagged Beatles files: every
  one was a chromaprint-confirmed copy of a studio-album track. Winners by
  measured cutoff (96 AAC, 48 incumbents); losers to `_superseded` (op
  `resolve-pairs`). The existing White Album Disc 2 rip carried Disc 1's
  titles; it was retitled from AcoustID and the official running order.
  Same script settled the doubled OMD Liberator folder.
- `library/tocollections.py` moved 18 various-artists records (327 files)
  from VariousArtists to Compilations (16) and Soundtracks (2), with the
  user's explicit authorisation for that list. Those folders are otherwise
  still off limits.

Later the same day: `library/ddcollection.py` folded the remnant of the
Duran Duran fan-club box (30 tracks: 13 new, 17 settled by quality) and
retired the "For Luna" folder; `library/foldcredit.py MAIN VARIANT` folds
one artist folder into another (whole albums move, shared albums are settled
track by track by fingerprint and cutoff, albumartist is set to the main
name) and was run for Joe Strummer & The Mescaleros, Johnny Clegg & Savuka,
the seven artists spelled two ways (Eagles, Gipsy Kings, The Killers, Pet
Shop Boys, The Shins, The Who, 'NSYNC) and every "Whatever, The" folder.
No artist folder is spelled two ways now.

Later still (evening of 1 Sep, second session): the last doubly-spelled
artist, "Orchestral Manoeuvres in the Dark (OMD)" (Live In Berlin only), was
folded with `foldcredit.py`; the 62 empty folder shells the day's moves left
behind (old OMD, Doors, Cranberries names and 30-odd singles) were removed,
plus the 11 artist folders that emptied with them. VariousArtists has 829
artist folders.

`library/resolve_unknown.py` gave the twelve "UnknownAlbum" folders the
Beatles treatment: chromaprint against the artist's own albums, measured
cutoff picks the winner, loser to `_superseded` (op `resolve-unknown`).
21 pairs settled, every one in favour of the album copy. With `--ident` it
also runs the leftovers through AcoustID and lets a stray JOIN an album folder
the artist already has, but only when neither its name nor the identified
recording reads as a remix, live take, edit or instrumental (AcoustID
fingerprints the first two minutes, so a club mix identifies as the single).
One joined: the Solitudes track into Natural Massage Therapy. AcoustID also
showed Elton John's "Greatest Hits/10 Border Song.m4a" is Crocodile Rock
(track 10 of that album is Crocodile Rock); it was retitled and renamed.

The 114 leftovers were then settled on the user's decision (1 Sep, late):
`resolve_unknown.py --assorted` moved 95 of them to `<Artist>/Assorted`, the
pool fold's name for an artist's stray tracks (Devo's went into its existing
"Devo-Assorted"), tags untouched like the other Assorted folders; and
`tocollections.py` gained one authorised entry, "Misc Swing" (19 tracks of
Indigo Swing, Royal Crown Revue, Big Bad Voodoo Daddy, Louis Jordan, Peggy
Lee and others, wrongly parked as an artist) which is now
`Compilations/Misc Swing`. No UnknownAlbum folder remains. Every leftover's
AcoustID identification is still in `library/acoustid_cache.db` should the
Assorted tracks ever be tagged properly.
`scan.py` still only knows the VariousArtists pool, so the 327 files moved to
Compilations/Soundtracks keep stale `files` rows until it learns those folders.

`etl.py` now carries `artist_top`, `artist_similar` and `artist_dz` (the
Deezer top-tracks and related-artists data behind artist radio) across a
rebuild, re-keyed on artist name like `artist_info`. They were not carried
before, so the day's rebuilds had silently dropped them and every artist
radio call logged `no such table: artist_top` (72 times). `artistextra.py`
was rerun to refill them.

### Karaoke follows the output (1 Sep 2026, late)

The user's report: pick a song for karaoke while a Sonos room is selected and
it sang out of the phone. Now `karLoad` looks at `OUT`: with a Sonos target
(`KAR.remote`) it skips the two `<audio>` stems and WebAudio entirely and
POSTs `/api/karaoke/{tid}/cast` with the room ips, the music/vocal setting
and a start position. The server renders that setting into an MP3 once
(`_kar_mix`: `mix-m1-v0` is the plain instrumental; `mix-m1-v1` adds the
guide voice at 0.22, `mix-m0-v2` is voice only) beside the stems in
`D:\MusicStems\...`, about 5.5 MB each, and hands the room the URL with
`sonos.play_url`, seeking after a short retry if `at` was given. Sonos plays
no Opus, hence the MP3. `GET /api/karaoke/{tid}/mix/{name}.mp3` also renders
on demand. Changing Vocals or Music while remote re-casts from the current
position after a 250 ms debounce (`karRecast`), which is a short hiccup on the
speaker and the price of not mixing in the browser.

The lyric clock (`karClock`) free-runs from an anchor and is corrected from
`GET /api/sonos/pos?ip=` (transport state plus position, two UPnP calls)
polled every 300 ms. A speaker reports whole seconds, so the anchor is only
moved when the reported second turns over and the prediction is more than
0.25 s off, or on a gross error (a seek landed, a stall); every correction is
eased over half a second so the word fill never jumps (rule 6). Play/pause,
seek and the map scrubber go over UPnP through the same `fanOut` as the main
player; closing the screen sends Stop and hands polling back to the main UI
(`startPoll`), which was suspended while singing so it would not chase the
karaoke stream. Untested on a live room as of this note: only the endpoints
were exercised, because a speaker was playing at the time. First thing to
verify on a real Move: the clock's bias (the singer's Lyrics +/- nudge covers a
constant offset, and it is saved per song).

Also: the Display size button stepped by what had been ASKED for rather than
what was shown, so on a TV capped at ~136% the stored request kept climbing to
200% and steps down only ate into the excess -- "stuck at max". `cycleScale`
now steps through the sizes the screen can show and wraps to 100% after the
largest, since a remote has no right-click to step down.

### One folder, four artists (1 Sep 2026, late)

Folding the OMD folders left the jukebox showing FOUR OMD artists, because
`etl.py` names an artist from each album's album-artist tag (artist tag as
fallback) and the folder's albums were tagged "OMD" (8 albums), "Orchestral
Manoeuvres in the Dark", "...(OMD)" and the misspelt "Manouevres" (the two
Singles discs). `library/unifyartist.py NAME --also ALIAS...` rewrites every
artist/albumartist under an artist folder to one spelling when the value is
the name spelled differently or a listed alias (joined "A / B" credits when
every part qualifies), leaves real featured credits alone and lists them,
and logs old values to `tag_backup` (op `unify-artist`). 245 tags rewritten;
the read model shows one OMD with 22 albums. Worth running for any artist
that shows twice in the A-Z after a fold. Run next for Paul Oakenfold, whose
Bunkka was tagged "Oakenfold" (2 Sep). A survey (group album-artist by artist
folder) found 50 folders whose albums name more than one album-artist; most
are real credits (Tom Petty and the Heartbreakers, Springsteen & the E Street
Band, CSN vs CSNY) and need the user's call, a dozen are plain misspellings
(Alanis Morisette, Neil Dimond, Missy Elliot, Mark Anthony, Matchbox 20/Twenty,
Debbie/Deborah Gibson, Yaz/Yazoo, a Japanese Céline Dion), and a few are
misfiled records (Jeff Beck albums under Beck, Louis Armstrong under Sam
Cooke, Mac Wiseman under John Prine, a "WwW.LanzamientosMp3.CoM" album-artist
under Duran Duran).

### The second television (2 Sep 2026)

Both TVs are Hisense, about two years old, running the "Browser" app in two
different versions. On the one under test the d-pad drives the browser's own
mouse cursor, so the page only ever sees mouse moves and clicks: no arrow
keys reach it and spatial navigation cannot switch on. That is a browser
mode, not something the page can change; `/diag` on that set will name the
browser version. Two quirks fixed from the same session:

- The idle takeover (`IDLE_MS`) opened Now Playing after 30 s without input
  while something played, which on a TV landed mid-browse several times a
  session. It is now three minutes.
- Tab and Shift+Tab walk the same focus ring the d-pad uses, in reading order
  (`tvStep`), and turn TV mode on like an arrow does, so a desktop keyboard
  now exercises exactly what a remote in key mode will. Before, arrows worked
  on the desktop (first press lit the ring) but Tab did nothing, since the
  focusables are not tabbable elements.

### Pressing the mic on an unprepared song (2 Sep 2026)

Used to open the karaoke screen on its progress view for a job that runs for
minutes, taking the user away from what they were browsing and dropping the
d-pad focus. Now `karPressTrack` opens the screen only when the song is ready;
otherwise `karQueueSplit` POSTs the prepare, reads the queue and shows a toast
("Splitting queued · Title · 3 songs in the queue", or "Already splitting"),
marks the row's mic busy and stays put. The Karaoke page still shows the
percentage for anyone who wants to watch.

### The split queue survives a restart (2 Sep 2026)

`karaoke.py` kept its job queue in memory, so every server restart threw away
whatever was waiting, and the restarts for the day's other fixes had done just
that once already and would have again with 31 songs queued. Now every job
that is queued or running is mirrored to `D:\MusicStems\_queue.json` (in the
stem store, outside the repo, like the stems) on each state change, and
`kar.restore()`, called by `server.py` at import, re-queues them in their
original order; a job caught mid-separation starts over. The 31 pending jobs
of 2 Sep were carried over by snapshotting `/api/karaoke/queue` before the
restart and re-POSTing prepare for each afterwards. A failed job was already
harmless: the worker records `error` and moves on.

Also found: stopping the server leaves its demucs child running as an orphan,
and two were found competing for the CPU. Restart procedure now kills any
`python ... demucs` process after stopping the server (the restored job
starts clean).

Alanis Morissette: 89 tags spelled "Morisette" unified, and the six tracks of
the Precious Illusions single had the album tag "Pression Illusions" fixed.

### "Lots running at once" on the Karaoke page (2 Sep 2026)

Not real. The Preparing list's 4 s tick only touched rows still in flight, so
a song that finished stayed on screen frozen at its last percentage until the
whole queue drained; with 31 queued that read as a dozen jobs running in
parallel, or abandoned. The queue itself was one-at-a-time and healthy. The
tick now removes a row the moment its song leaves the in-flight list (and its
artist heading when empty) and redraws only the Ready list and the count
beneath it. Error rows read "failed" from the first paint. Seven empty
`*.tmp` folders left in the stem store by earlier interrupted runs were
removed; a running job's tmp folder is normal and is deleted on failure.

### The Cure's Greatest Hits looked duplicated (2 Sep 2026)

36 tracks, every title twice. Not a duplicate: chromaprint put every pair at
0.37-0.47 apart (copies score under 0.15), and AcoustID's full release-group
lists placed tracks 19-36 on "Acoustic Hits", the bonus disc of the 2001
release, with several recordings titled "(acoustic live)". The two discs had
come from the two Cure folders ("The Cure" and "Cure, The") and were joined
as extras by the fold. Fix, tags only, op `disc-fix`: disc 1 on the originals,
disc 2 with tracks renumbered 1-18 and " (Acoustic)" appended to the titles on
the bonus disc, files renamed "2-NN - Title (Acoustic).mp3". The read model
shows one album with disc_count 2. Subfolders per disc were not used because
`etl.py` makes every folder its own album (the OMD Singles' Disc 1 / Disc 2
are two albums in a collection); disc tags keep it one album.

Rebuilding the read model no longer needs a server restart: `db()` opens a
fresh read-only connection per request, so the new file is picked up at once
(ids change, so a browser reload is still needed). Restarting would have cost
the running separation job.

### Auto-start and backups (2 Sep 2026)

`jukebox/serve.ps1` is a restart loop around `server.py` with log rotation
(`logs/server.log`, `.1` to `.5`, plus `launcher.log`); it refuses to start
if the port is already owned, and stops when `logs/STOP` exists.
`register-task.ps1` registers it as Scheduled Task "MusicMate": at logon,
hidden, no execution time limit (schtasks' default is three days), restart
3x at 1 min, runs on battery. Boot-time needs an elevated shell and stored
credentials; the script's header has the commands. Consequence for every
later fix: restart the server by killing python, never by starting another.

`server.py` backs `state.db` up daily through SQLite's online backup API
into `jukebox/backups/state-YYYYMMDD-HHMM.db`, five kept, checked hourly by
a daemon thread and at startup (`backup_state`). It is the one file that
cannot be rebuilt.

### System page (2 Sep 2026)

`#/system` (nav pill "System") reads `/api/admin/status`: build (git hash and
date, taken once at startup), uptime, address, read-model counts and build
time, library.db files/pools/last scan/unfingerprinted, karaoke store and
queue, lyrics cache counts, state.db size and backups, known rooms, launcher
and error log tails. Buttons: **Rescan now** (POST `/api/admin/rescan` with
the ticked folders; runs `musiclib.py scan <roots>`, `musiclib.py
fingerprint`, `etl.py` as subprocesses in a thread, log polled from GET
`/api/admin/rescan`, ends with a Reload prompt because ids change), **Back up
now**, **Refresh speakers**, **Restart server** (`os._exit(0)` after 0.6 s;
the launcher restarts it, `kar.restore()` re-queues splits).

The scanner's "gone" deletion is now limited to the roots it walked
(`musiclib.cmd_scan`); before, scanning VariousArtists alone would have deleted
the Compilations and Soundtracks rows. Only VariousArtists is ticked by
default: the library holds just the 20-odd collection albums moved there by
hand, and ticking Compilations or Soundtracks would bring those folders in
whole. Whether the jukebox should include the collection folders at all is
an open question for the user; scanning them is read-only and allowed, the
rule is about modifying them.

### Quality of life, batch one (2 Sep 2026)

- **Sonos pair with a missing half.** `sonos.invisible(ip)` parses the
  topology for members marked Invisible and whether their ChannelMapSet
  partner is present. `_sonos_rooms_build`'s fallback marks such a device
  `unavailable` with the reason; the output sheet shows it greyed, unselectable,
  and taps toast the reason. When the pair is whole the room appears normally.
- **Karaoke.** Queued (and failed) rows on the Karaoke page carry a ✕ that
  DELETEs `/api/karaoke/prepare/{tid}`. Every split this browser asks for is
  remembered in `localStorage` (`jb.karwatch`) and polled every 20 s until
  ready, then announced once with a toast and the row mics updated.
- **Recently added** shelf on Home from `/api/recent-added`, which orders by
  `album.added`. `etl.album_added` takes the earliest of creation and
  modification time across the folder's files: modification time alone made
  every retagged album look new; creation time survives copies, renames and
  tag writes on NTFS (the Cure's Greatest Hits reads 2012). Distribution now:
  2005, 2009, 2012 and the late-August staging batch, as expected.

### Quality of life, batch two (2 Sep 2026)

- **Playlists reorder by drag.** `wireGrip(grip,row,list,opts)` now takes a
  row selector and an `onDrop(rows)` hook (the queue keeps its old behaviour);
  `vUserPlaylist` gives each row a grip and posts the skey order to
  `/api/pl/{pid}/order` on drop.
- **Sleep timer** (moon button on Now Playing): 15/30/45/60/90 min or "after
  this song". On a Sonos room the speaker's own timer is set
  (`sonos.sleep_timer`, ConfigureSleepTimer) so it fires even if the phone
  sleeps; locally a timer fades the volume over ten seconds and pauses, and
  "after this song" is honoured in the `ended` handler.
- **Crossfade** (button beside it, remembered in `jb.xfade`): on Sonos it is
  the speaker's own setting (`sonos.crossfade`, SetCrossfadeMode); locally
  there is one `<audio>` element, so it is a fade, not an overlap -- the last
  three seconds ease down, the next track eases in over 2.5 s.
- **Refetch cover** on the album page: POST `/api/admin/refetch-art/{alid}`
  runs `library/fixart.py` for the folder (MusicBrainz + Cover Art Archive),
  points the read model's album row at the new file, drops the served
  thumbnails; the client adds `&r=` to that album's art URLs for the session.
- **Nightly lyric pre-warm**: a daemon thread runs `warm_lyrics` at 4 am,
  1,500 unknown songs a night with a 0.5 s gap (LRCLIB is a free service),
  newest albums first; status on the System page with a "Warm 300 now" button.

Lesson from this batch: a top-level IIFE that called `regChrome` before it was
defined threw at load and left the page blank below the header -- every
later top-level statement, route() included, never ran. New top-level code
must reference only what is defined above it, or run on `load`. Headless
Chrome with `--enable-logging=stderr` shows the console error; a blank
screenshot is the tell.

### Favourites as a small library; System behind a gear (2 Sep 2026)

System left the nav for a gear icon (`#tsystem`) in the top-right group.
`vFavs` was rebuilt: header with counts, Play / Shuffle / "Add songs…"
(opens search; starring still happens where the music is), then three chip
views remembered in `jb.favview`: Songs (rows whose + is a ✕ that unstars),
Albums (albums starred whole, each with a ✕, then albums derived from the
starred songs with "N starred songs"), Artists (likewise). This is also the
shape a sub-library view would take, see below.

Proposed, not built: **sub-libraries by top-level folder**. `etl` records
`album.pool` (the folder under D:\Music); the main browse (Artists, Albums,
Genres, Eras, search) is scoped to VariousArtists; every other pool present
in library.db shows on Home as a card beside the Mixes and opens at
`#/lib/<pool>` with the same Songs / Albums / Artists chips. The Rescan
folder ticks then bring Christmas, Soundtracks etc. in as their own
sub-libraries instead of mixing into the main one. Side effect to agree
first: the twenty-odd compilation albums moved by hand would leave the main
browse for the Compilations sub-library.

### Sub-libraries (2 Sep 2026)

Built on the user's decision that Compilations and Soundtracks are libraries
of their own, not part of the tidy main one. The unit is the top-level folder
under D:\Music, recorded as `album.pool` by `etl.py` (`MAIN_POOL =
'VariousArtists'`). Rollups follow: `artist.n_albums/n_tracks/duration` and
the genre/subgenre counts cover the main pool only, `artist.all_albums /
all_tracks` cover everything, `art_album_id` prefers a main-pool cover, and
the `search` FTS table carries `pool`. An artist with no main-pool album is
kept (their albums need them) but hidden from the main Artists list by
`n_albums > 0`.

Server: `pool_where()`; `/api/artists`, `/api/albums` take `pool=` (none =
main, `*` = all, a name = that sub-library, with counts recomputed from its
albums); letters, by-letter, axes, genres, decades, recent-added are main
only; `/api/search` returns `pool`; `/api/artist/{aid}` albums carry `pool`
and list main first; new `/api/pools` (each with four cover ids) and
`/api/lib/{pool}/tracks`.

UI: Home shows a "Libraries" strip of cards beside the Mixes; `#/lib/<pool>`
(`vLibrary`) has Albums / Artists / Songs chips remembered per pool, Play and
Shuffle, and streams the song rows a chunk per frame. Search lists the main
library first, then "In Compilations" etc. Favourites resolve against
`loadAlbumsAll/loadArtistsAll` (`pool=*`) so a starred soundtrack still
matches. Result today: main 2,546 albums / 852 artists; Compilations 17
albums; Soundtracks 2. Ticking a folder on the System page's rescan brings it
in as a new sub-library.

### Era, Christmas, Compilations, Soundtracks brought in (2 Sep 2026)

Via the System page rescan, two runs (the second refused while the first
ran; the UI toasts "a rescan is already running"). Libraries now: main 2,546
albums / 849 artists; Era 62 albums / 4,566 songs (one folder per Billboard
year); Christmas 54 / 832; Compilations 134 / 2,578; Soundtracks 119 / 2,051.
Fingerprinting 4,800 new files took ~8 min at ~9/s, the scan itself ~100 s.

Two rules learned from Era: (1) `etl.album_credit` -- with no album-artist
tag on most tracks, a folder naming four or more performers with nobody
holding half becomes "Various Artists" (`is_compilation=1`, per-track
performers kept); in the main pool such a folder is credited to its artist
folder instead (DJ mixes, tributes). "Various"/"VA"/"Various Artists" all map
to the one name. (2) The rollups in `compilations.py` and `album_tidy.py`
recomputed `artist.n_albums` over every pool and put 84 soundtrack- and
Christmas-only names into the main Artists list; both are pool-aware now and
also maintain `all_albums/all_tracks`. Any new derivation step that touches
artist counts must do the same.

Loose ends for the user: a stray "The Who - Teenage Wasteland.mp3" at the top
of Era shows as a one-track album; Soundtracks has 18 albums credited to
"Soundtrack" and 15 to "Original Soundtrack" from their tags (a tag pass
could fold them into Various Artists); Compilations has "Various Artists -
Blues/African/Irish" as separate names.

### Random covers after a rescan (2 Sep 2026)

`ART_GEN`, the token that keys the server's cover cache and every art URL
(and that `/api/build` reports so open pages reload after a rebuild), was
computed once at startup. The System page's rescan rebuilds the read model
WITHOUT a restart, ids are reassigned, the token stays, so browsers kept a
year's cache of covers under ids that now belonged to other albums, and the
reload prompt handed them the same stale token. Now `art_gen()` stats the
database on each use (the cache key, the stream token check, `/api/build`,
the page injection, the refetch glob); a rebuild changes it at once, the
minute-and-focus `checkBuild` poll sees the change and reloads. Any new use
must call `art_gen()`, never the startup constant.

### Covers for the new libraries (2 Sep 2026)

`library/artfetch.py` (Deezer, then iTunes; refuses doubtful matches) had only
ever run over VariousArtists, so Christmas, Compilations, Soundtracks and Era
came in mostly coverless and the user was pressing "Refetch cover" album by
album. Now: the System page's rescan runs `artfetch.py survey` and
`artfetch.py fetch` before the rebuild (checkbox, on by default), and a
**Fetch missing covers** button runs just those two steps plus the rebuild
(POST `/api/admin/covers`, same job runner and progress panel). The first
run was started from the CLI on 2 Sep: 360 folders surveyed, 1,095 albums
under 600 px to look up (that includes small covers in the main library).

### Era standardised (2 Sep 2026, on the user's explicit instruction)

Era is outside VariousArtists; the user named it, so it was modified.
`library/era_tags.py`: every Era/<YYYY> file gets album "Billboard Top Hits of
<YYYY>" (one prefix so the years sort), albumartist "Various Artists", date =
the folder's year, and +1 on track numbers where a folder counted from 0
(1956, 1957, 1959). 4,565 album tags, 4,562 album-artist, 472 years, 300
track numbers; titles, performers and genres untouched; op `era-tags` in
tag_backup. `library/era_covers.py` draws a 1000 px sleeve per year folder --
BILLBOARD / the year / TOP 100 HITS or TOP 30 HITS by the folder's count,
colour by decade -- into the art cache as `Era__YYYY.jpg` and registers it in
artcache.db as a hit (url `generated:era`), so etl copies it and artfetch
leaves it alone. Re-run to redraw. The lone "The Who - Teenage Wasteland.mp3"
at the top of Era was left for the user to file.

### Karaoke word timing by forced alignment (2 Sep 2026)

The complaint: lines started roughly on time but the words inside raced and
then waited. Cause: the screen only ever knew LINE starts (LRC) and guessed
the words from the envelope. Now `jukebox/align.py` (runs in the karaoke
venv; torchaudio's MMS_FA CTC aligner, ~1.2 GB model cached under
~/.cache/torch) aligns each LRC line's exact words to the vocal stem inside
a window around its timestamp and writes `words.json` beside the stems:
per line `i` (LRC index), `c`, `ok`, and per word `s`/`e`/`c` plus `u`, the
units under the word (characters now; phonemes later for the singing-robot
project -- same file, k="phone"). ~80 s per song on CPU.

Trust is decided per line by `ok`, not by the raw score: the model's
probabilities run low on singing (median ~0.2 on ABBA), so a line is doubted
only when far below the song's own median, placed nowhere near the LRC time,
or packed/stretched beyond singing. Measured on Dancing Queen: aligned first
words a median 0.04 s from a vocal onset (26/36 within 0.15 s); the LRC's
line times 0.67 s off (0/36) -- the LRC was made for another master, and the
aligner corrects it, which also makes the per-song nudge mostly unnecessary.

Wiring: `karaoke.align_stem` runs after every separation (state 97 %,
"aligning words"; a failure leaves the song singable); `enqueue_align` /
`ALIGN_ONLY` queue entries align songs split earlier, one at a time in the
same worker, restored across restarts; POST `/api/admin/align-missing`
queued all 255 ready songs on 2 Sep (~6 h of CPU behind the split queue).
GET `/api/karaoke/{tid}/words` serves the file (no-store). `karBuild` takes a
line's word starts from it when `ok` and the word count matches, moves the
line start to the first word and the end to the last, and skips the onset
snapping for those lines; everything else falls back as before.

### The smoke backdrop vanished; Preparing list broke (2 Sep 2026)

Two consequences of the day's background work. (1) The WebGL backdrop
demotes itself when 90 frames average over 55 ms and gives up for good at
tier 3, remembering `jb.gltier=9`. With demucs and the aligner holding every
core, a browser on the server machine measured slow and lost the smoke
permanently. Now: GLPROBE bumped to 3 (one fresh retest everywhere), a
demotion expires after a day (`jb.gltier_at`), and a tier only drops after
three slow windows in a row. (2) The cancel ✕ on Preparing rows was appended
as a sixth child of a five-column grid row and wrapped onto its own line (the
playlist grip made the same mistake earlier); it now reuses the row's + slot
as the playlist page does. Alignment jobs, which are not something anyone
waits for, collapsed from a row each into one "Word timing: aligning X · N
more" line above the split rows.

### Backdrops and the Preferences sheet (2 Sep 2026)

The gear now opens a Preferences sheet (`openPrefsSheet`; the System page is
linked from its foot and still lives at `#/system`). Backdrop: six styles --
Smoke (the shader), Gradient (the old fallback, now a choice), Sleeve (the
cover blurred and drifting), Aurora (three ribbons), Embers (motes, brighter
with the music's energy), Grain (vignette, film grain, turning grooves).
All but Smoke draw on `#bd2d`, a 2D canvas at half resolution, and all take
their colours from `GL.cur`, the same eased palette the smoke uses, so
`paletteFromAlbum` lights every style; Sleeve also loads the cover through
`bdSleeveSrc`. Motion: Full / Calm (half speed) / Still (frozen; also what
prefers-reduced-motion selects) through one clock, `bdTime`. Idle takeover:
1 / 3 / 10 min / Never (`jb.idle`, read by `idleMs`). Display size as a
segment. Choices are per device: `jb.backdrop`, `jb.motion`.

When the frame test demotes WebGL, the choice stays Smoke but `bdEffective`
draws Gradient and the sheet says so in amber, with the retry-tomorrow note.
`?prefs=1` opens the sheet on load for headless checks. A mock of the seven
candidates (Plain was dropped) is the artifact "MusicMate Backdrops".

### The television app, port 80, and a name for the server (3 Sep 2026)

**tvapp/** is an Android TV wrapper: `MainActivity.java`, one full-screen
WebView (JS, DOM storage, autoplay without a gesture, cleartext HTTP, screen
kept awake, Leanback launcher entry with a drawn banner). The D-pad reaches
the page as arrow keys, so the web app's TV mode does the rest. Back on the
remote calls the page's `backAction()` and never leaves the app; a long press
of Back opens the address dialog. It tries, in order, `http://music.localdomain/`
(the UniFi A record, 23 Sep 2026), `http://music/`, `http://192.168.1.50/`, `http://192.168.1.50:8730/`,
then asks; a typed address is saved. minSdk 21, targetSdk 34, no libraries.

Build: `tvappuild.ps1` (portable JDK 17, Android cmdline-tools with
platform 34 and build-tools 34.0.0, Gradle 8.9, all under `D:\Tools`,
no admin). The first run made the self-signed key
`tvapp/app/keystore/musicmate.jks` (gitignored, password musicmate): KEEP A
COPY -- a TV refuses an update signed with a different key and the app must
be uninstalled first. Output lands in `jukebox/dist/mm.apk` (about 30 KB)
and the server serves it at `/mm.apk` (the long name `/musicmate.apk` still works) with the APK media type. On the
TV: install "Downloader" (AFTVnews) from the Play Store, allow it to install
unknown apps when Android asks, enter the address the System page shows,
Install. The System page's "Television app" card shows the URL and build time.

**Port 80.** Nothing else listened on it, the firewall rule is per program
(python.exe, any port), and binding 80 needs no admin on Windows, so
`serve.ps1` now defaults to 80; the task was restarted. Sonos stream URLs
follow (`lan_host()` uses `PORT[0]`). 8730 still works if started by hand.

**A name instead of the IP.** On the UniFi Network application: Settings ->
Routing -> DNS (older versions: Settings -> Networks -> the LAN -> DHCP,
"DNS Server" left as the gateway) -> Create Entry, type A Record, name
`music`, IP 192.168.1.50. Clients that take DNS from the gateway then reach
`http://music/`. Give the PC a fixed IP first (Client Devices -> the PC ->
Settings -> Fixed IP Address) or the record goes stale. Browsers treat a bare
`music` as a search; `music/` or `http://music/` works. The app tries the
name first, so once the record exists nothing else needs changing.

### Karaoke word timing, version 2 (23-24 Sep 2026)

The user: Fernando's timing was "utterly" wrong -- right at times, then the
wrong line or words shown, and stuck three words before the end of a line
while the next was sung. Cause: `align.py` v1 cut each line's window from
the LRC's own time (0.8 s before it, 0.6 s after the next). Fernando's LRC is
1.5 s late, so every line was pinned against the window start and its tail
spilled onto the next line's audio. Over 325 songs per-song drift piled up
against the -0.8 s wall; 70 songs are more than 0.8 s out.

**`align.py` v2** (details in its docstring): emissions once per song;
pass one finds each line's drift in a wide window (local median of nine,
ignoring lines whose words repeat nearby -- a chorus twin pulled Because the
Night 1.5 s early); pass two aligns each line in a block with its neighbours
and MMS star tokens between; a line is `ok` when its three block placements
agree within 0.15 s, else the best-scoring of block / near-drift / near-LRC
windows is kept and marked `fair` if sane; word starts snap to a voice rise
out of silence (the star swallowed vowel attacks, "I" landed 0.2-0.3 s late)
and skip stray leading letters only where the voice really restarts; word
ends follow the envelope; numbers are spelled out. Per line: `d` (drift),
`seen`, `stable`, `how`, `ok`, `fair`. ~90 s a song.

All 325 split songs were re-aligned through the server queue on 23-24 Sep
(`karaoke.aligned()` now means version >= 2, so `/api/admin/align-missing`
re-queues old files). Each v1 file is kept as `words.v1.json`.

Screen (`karBuild`): trusts `ok` or `fair`; a doubted line uses its own `d`
(less the 0.25 s reading lead) instead of the song median; a moved line's
words start filling LEAD after it appears (they filled 0.25 s early); a
words.json line is used only if its text matches the screen's line.

**Lyrics parsing** (`lyrics.parse_synced`, shared by align.py and the tools
so line indexes match the screen): several timestamps on one line become one
line per time (they were shown as text and the repeats were missing -- 3 of
22,274 files, Because the Night the only split one); `<Instrumental>` and
`<Bridge>` are gaps; `[on the]` keeps its words. Needs a server restart.

**Measuring** (all in `jukebox/tools`, system Python):
- `discref.py`: human word timings from the CD+G discs, carried onto the
  song's clock by chroma+onset DTW of the disc's backing MP3 against
  `backing.opus`. 126 songs; discs agree with each other to a median 0.06 s.
  Covers can phrase differently (the user's caution), so words count only
  where two or more discs agree within 0.25 s. Cache: `jukebox/cache/discref`.
- `align_eval.py`: scores lrc / v1 / v2 against those references, placing
  words as the screen would (`--fair`, `--only-v2`).
- `stem_eval.py`: scores line starts against where the voice comes out of
  silence in the stem -- no outside reference. Partly circular for v2 now
  that it snaps to voice rises; its gross-miss rate is the useful number.

Results, 24 Sep 2026 (v1 -> v2, screen as shipped):

| | v1 | v2 |
|---|---|---|
| disc: lines > 0.5 s out (62 songs) | 25.5% | 13.4% |
| disc: lines within 0.3 s | 62.6% | 74.0% |
| disc: words > 0.5 s out | 19.3% | 15.9% |
| stem: phrase starts > 0.5 s out (324 songs) | 12.8% | 15.3% |

Open: the stem judge's gross phrase-start misses got worse while the disc
judge's line starts halved; find those lines before tuning further. Fernando
and Because the Night were checked on the TV by the user (Fernando
"perfect"). Fernando still has a saved -200 ms offset from the v1 days, as
do 16 other songs; they are the user's settings and were left alone.

### Word timing version 3 (25 Sep 2026)

Two findings after v2 went live, each fixed and re-queued (ALIGN_VERSION 3):

1. **The phrase-start regression was in the lines v2 doubted.** On trusted
   lines v2 beat v1 (131 v1-only misses against 52); the loss was all in
   doubted lines, mostly "crossed" -- and much of that was self-inflicted:
   the onset snap could pull a line's first word back over the previous
   line's last one. Crossings are now judged before any tidying, only the
   unstable line of a crossing pair is doubted, and the snap cannot cross the
   previous line. A star-less v1-style placement around the corrected time is
   also computed for doubted lines (`alt`), measured, and NOT used: it helps
   words (12.2% vs 13.5% over half a second) but hurts line starts (13.3% vs
   12.0%), and line starts are what the user saw go wrong.
2. **Some LRC files are out by more than pass one can see (3 s).** The user
   had nudged Come Undone +5 s and To The Shore +10.6 s by hand. Pass zero
   (`global_offset`) slides the LRC's line starts over +-15 s against the
   voice's onsets, and the aligner's own fit picks among the best few (music
   repeats every bar). A shift beyond 3 s is kept only if it leaves more
   lines trusted than no shift does (Lyle Lovett, My Baby Don't Tolerate:
   -5.2 s with 6 of 26 trusted, rejected for 14 at 0). Verified: Come Undone
   +11.05 s (the vocal stem is silent until 44 s, the LRC starts at 33.6 s;
   the discs agree once allowed to look that far), To The Shore +11.0 s
   (nudge +10.6), Love Shack +2.2 (nudge +1.8), Fernando unchanged.

**The disc judge is blind to files more than 4 s out**: it matches a disc
word only within `--tol` (default 4 s) of the LRC, so for such a song it can
only match wrong words, and scores the raw LRC as right. Use `--tol 13`
when checking a big shift.

Saved per-song offsets (17): several were the user compensating v1's error
and now double-correct (Fernando, Love Shack, Code Monkey, Girls On Film,
OMD History of Modern, Planet Earth, To The Shore, Come Undone). They are the
user's settings; reset only with their say-so, after the v3 re-run.

### Wrapper fixes (23 Sep 2026, APK 1.1, versionCode 2)

A review of `MainActivity.java` found, and this build fixes:

- **No `WebChromeClient`**, so the WebView swallowed `confirm()` and `prompt()`
  (confirm answered false, prompt null): Clear queue, New/Rename/Delete
  playlist and Restart server did nothing in the app. The stock client now
  shows Android's own dialogs.
- **Address search skipped candidates.** A failed load reports twice (the
  error, then `onPageFinished` on the error page) and each report stepped on.
  A load generation now gives one verdict per load.
- **An error after the app had loaded was permanent** (Chrome's error page for
  good). The wrapper now retries the same address every 4 s.
- **`configChanges`** now covers keyboard, navigation, uiMode, screenLayout,
  smallestScreenSize and density, so a remote or keyboard reconnecting no
  longer recreates the Activity (and the page, and the music).
- **Long-press Back** checks `FLAG_CANCELED_LONG_PRESS`, so opening the address
  dialog is not also a Back.
- Server name is `music.localdomain` (the UniFi DNS record) and is tried first.

Page side: `MediaPlay`, `MediaPause`, `MediaStop`, `MediaFastForward`,
`MediaRewind` are handled (`mediaKey()`), Play/Pause set a state instead of
toggling (`setPlaying`), and `ChannelUp`/`ChannelDown` scroll like Page keys.
`tvnav_check.py` artists-rail fails before and after these changes (step 5,
Left from the rail stays on the rail); not yet investigated.

### First run on the television (3 Sep 2026)

The wrapper installed and ran on the Hisense via Downloader. Fixed from the
photos: (1) the WebView reported 960 CSS px (a 1080p set at density 2), so
the nav ran off the right edge and the size cap allowed only 100% -- `index()`
now serves `<meta viewport width=1280>` to the MusicMateTV user agent
(the layout's design width; setUseWideViewPort scales it to the screen), and
the nav tightens below 1180/1040 px as a backstop; `IS_TV` matches the app's
UA. (2) The now bar's "This device" button was not d-pad focusable
(`.outbtn` registered now) and shows elapsed / total time (`#nbtime`,
painted by `paintProgress`). (3) Preferences has "Send this screen's
diagnostics" (`sendDiag`, the `?diag=1` code as a function) so a fixed-URL
TV app can report geometry to `/api/diag`. Still to diagnose from that
report: the Now Playing header overlapping the cover on the set.

Lyrics for compilation tracks were looked up as "Various Artists": the three
lookups (`get_lyrics`, `warm_lyrics`, the nightly `_warm_todo`) now use
`COALESCE(track.track_artist, artist.name)`, and 821 cached misses for
compilation-track skeys were deleted so they are asked again. "Shake Your
Love" (Debbie Gibson, Era 1988) went from no lyrics to 81 timed lines.

### Per-song sleeves on compilations (3 Sep 2026)

The Billboard year albums keep their generated sleeve as the album cover, but
the song that is playing shows its own. `library/trackart.py --pool Era
--apply` asks Deezer by performer and title (iTunes as fallback), gates hits
with `artfetch.artist_ok` plus a title ratio of 0.80, prefers an album to a
single and demotes hits collections, scales the cover to 600 px and embeds it
(`embedart.set_art`), sets `files.has_art`, and records every attempt in
`artcache.db.trackart` so misses are not re-asked. Only Era and
VariousArtists may be written (`--force-pool` otherwise). On the 1988 sample:
13 of 14 hits. The full Era run (3 Sep, 7,000 s -- iTunes fallbacks are
slow) found 4,146 of 4,562; misses are recorded. The 1960s sets had the
performer inside the title ("The McCoys / Hang On Sloopy", artist "Various"):
`era_tags.py` now splits those into artist and title (160 files) and the
decade was re-run with `--folder Era/196 --redo` (the folder option is a
prefix). Debbie Gibson's "Shake Your Love" shows Out of the Blue in Now Playing.

Read model: `track.has_art` (etl). Server: `GET /api/track-art/{tid}` (the
file's embedded picture, cached as `t{gen}_{tid}_{size}.jpg`); track rows
from `/api/album`, `/api/tracks`, `/api/lib/{pool}/tracks` carry `has_art`,
`is_compilation`, `pool`; Sonos DIDL art uses it too. UI: `usesTrackArt(t)`
(has_art and a compilation or non-main pool), `trackArt/trackImg` used by
Now Playing, the now bar, the media session, queue rows, track thumbs and the
karaoke header; `paletteForTrack` lights the room from the song's sleeve.
Album tiles and pages are untouched. Ordinary albums never take this path.

### D-pad navigation model (3 Sep 2026)

`tvMove` used one distance score (`p + o*2.6`) over every visible stop, so
Right could climb into the nav from an album's action row, land on Home from
Artists, or jump from a song to a chip. Rules now:

- Left/Right stay on the LINE: only stops whose vertical extent covers the
  focused element's centre count; nearest in that direction wins; none means
  the focus stays put (no wrap, no region jump). A two-column track list is
  one line per row, so Right from the left song is the right song.
- Up/Down: nearest stop whose centre is beyond ours, preferring horizontal
  overlap, then distance; crossing regions vertically (nav -> header buttons
  -> chips -> list) is the intended path.
- A track row's mic and + are stops on its line (`tvRowButtons`): Right from
  the row steps into them, Right from the last leaves the row, Left steps
  back out; the row keeps a soft highlight (`.focrow`) while a button has the
  ring; Enter clicks whatever holds it.
- Text boxes: Up/Down blur and move (and switch TV mode on if the first arrow
  arrives in a box); Enter on a box puts the caret in it. The karaoke filter
  is a registered stop and is not auto-focused in TV mode. The A-Z rail
  letters are stops on every device (Right from a right-column row, Left back).

Not verifiable headless (no key events): checked by reasoning and a TV-mode
screenshot only. Watch for: rows whose centre line just misses a neighbour's
extent (the 4 px slack), and pages where a stop is wider than the viewport.

### Karaoke page by artist; a sung song is the current song (3 Sep 2026)

`vKaraoke` Ready and could-be-next lists are built by `mkGroups`: one block
per artist (`.kargroup`: photo from `loadArtistsAll` when the artist has one,
else their cover, else the first song's sleeve; name; count) with the songs
beside it as single lines (`.krow`: title, album, duration) in three columns,
two below 1100 px, one on a phone. A compilation's songs group under the
performer (`whoOf`); a band's album stays under the band. Filter and A-Z rail
walk `.kargroup`/`.krow`. Preparing keeps the old rows (they carry pct and ✕).

`karAdopt(t)` (called from `karOpen`) makes the sung song the player's
current one: queue of one, paused, painted, play-history started -- the now
bar and Now Playing agree with the room. `togglePlay` defers to
`karTogglePlay` while the karaoke screen is open so the bar's play button
cannot start the ordinary stream under the stems.

Artist tiles in TV mode: `content-visibility:auto` containment clipped the
focus ring and swell to the tile box; the focused tile lifts containment.

### Sing them all, and joining a song (3 Sep 2026)

"Play the mixes" (played the prepared songs as ordinary music) is gone.
**Sing them all** (`karParty`) shuffles every ready song and opens each in
the karaoke screen as the last ends: `karEnded` (the local stem's `ended`
event, or the speaker reporting STOPPED near the end) toasts the next title
and hops after 2.8 s via `karClose` with `KAR.partyHop` set, so the hop does
not end the run; Back or Escape does (`karClose` clears `KAR.party`). A song
that is not ready or has no lyrics is skipped. **Join in**: the mic on Now
Playing passes the current position (`nowPos()`) to `karPressTrack(t, at)`
-> `karOpen(..., at)` -> `KAR.startAt`; locally `karSeekTo` once the stems
play, on a speaker the cast starts there. Row mics still start from the top.

### Back on the remote, and where the ring lands afterwards (3 Sep 2026)

The TV app sends Back straight to `backAction()`, which knew nothing of the
karaoke screen, so Back there only moved the ring. `backAction` now closes,
in order, the karaoke screen, any open sheet, search, Now Playing, and only
then steps back a page, setting `NAV.back` so `route()` puts the ring on the
current section's nav pill (`tvFocusSection`) instead of the first element;
`closeNP` and `karClose` do the same. Forward navigation still lands on the
first stop. On the Karaoke page the artist block (`.karart`) is a d-pad stop
in its own column, so Left from a first song reaches it and Up/Down move
artist to artist.

### Sonos: "Sonos Moves" would not play (1 Sep 2026)

Not a cache. "Sonos Moves" is a stereo PAIR of Sonos Move units. The one on
the network (192.168.1.51, `RINCON_000000000001`) is the left channel and is
marked Invisible in the zone topology; its partner, the right channel and the
pair's coordinator (`RINCON_000000000002`), was not on the network at all
(asleep, off, or out of Wi-Fi range), so the household topology had no
"Sonos Moves" room. `_sonos_rooms_build` still listed it through its
"player whose system answered nothing" fallback, and a queue sent to an
Invisible satellite is refused. R2D2 (a single Move 2) played fine. Fix in
the house: wake or dock the other Move. Fix in code, not yet made: the
fallback should skip a device the topology marks Invisible, or list it as
unavailable with the reason.
`scan.py` only knows the VariousArtists pool, so the 327 moved files keep
their `files` rows (src = Compilations/Soundtracks) until a rescan is taught
about those folders.

### Staging ingest: ToBeProcessed into the library (8 Sep 2026)

`library/ingest.py` brings albums from the staging folder
`~/ToBeProcessed` (Artist/Album (Year)/[CD 01/]files) into
the library. The folder tree is the trusted evidence: it supplies album artist,
album title (edition suffixes dropped), original year, disc and track numbers;
tags fill the rest; AcoustID is asked about any file whose artist or title had
to be inferred or disagrees with the folder, and an album is left in staging
when it disagrees on more than 30% of what it identified (`--force` overrides
after review). Every file is chromaprinted and compared with the library's
fingerprints: a match inside the same album keeps the higher-bitrate copy and
retires the other to `_superseded/incoming` (op `ingest`, undoable with
`musiclib.py undo --op ingest`); a match elsewhere (Era, a compilation) is only
reported. Art: existing embedded art if it is 500px or better, else Cover Art
Archive by the release group the Billboard work already mapped
(`jukebox/charts/billboard-all-distinct-albums.tsv`), else a MusicBrainz
search; the full cover goes to `library/cache/art` and 600px is embedded.
Files land as `<Pool>/<Artist>/<Album>/[Disc N/]NN - Title.ext`, are registered
in `library.db` with their fingerprints, tag changes go to `tag_backup` (op
`ingest`, path = destination), and the emptied source folders are removed.

First run: 439 albums, 6,508 tracks moved, 112 incoming duplicates superseded,
118 library copies replaced by better incoming ones, 4 albums entirely and 15
partly already present. One `Various Artists` soundtrack went to `Soundtracks`;
everything else to VariousArtists. 17 albums found no cover (Ye, Radiohead,
Tame Impala, Lady A, P!nk among them). Two artists now exist twice because the
incoming spelling differs from the library's: `P!nk` beside `Pink`, `Ye` beside
`Kanye West`. The user chose `Pink` and `Kanye West` as the standards (8 Sep 2026);
`foldcredit.py` folded the other folders in (op `fold-credit`) and
`unifyartist.py` rewrote the `P!nk`/`Ye` tags (op `unify-artist`). `Sia`/`SiA` and `Lady Gaga`/`Lady GaGa` tags were unified with
`unifyartist.py` (op `unify-artist`); the folders keep their old casing because
NTFS treats the two as one name.

Second drop (10 Sep 2026): 257 albums / 4,089 tracks staged; 256 albums /
4,066 tracks ingested, no duplicates of anything already held (Kate Bush's
Hounds of Love and Wham!'s single overlap Era/compilation copies, reported
only). Changes made to `ingest.py` for it: the script forces UTF-8 on
stdout (an `∞` in an album title crashed a redirected dry run under cp1252);
an `ALIASES` map applies the user's settled spellings at ingest time (`P!nk`
-> `Pink`, `Ye` -> `Kanye West`) for the folder, the album-artist, the
per-track artist tag and, through `nart()`, the AcoustID agreement check,
which had otherwise blocked Beautiful Trauma because AcoustID answers "P!nk";
`"<Film> - The Album"` counts as a soundtrack title, so Barbie and Twisters
went to Soundtracks rather than Compilations; and `--skip-pool <Pool>` leaves
albums bound for a pool in staging and reports them HELD. Bastille's Bad
Blood was blocked by one wrong AcoustID answer (Pompeii MMXXIII identified as
a Perturbator/Ulver track) and pushed through with `--artist Bastille
--force`. Eight albums found no cover through the MusicBrainz route; the
Deezer/iTunes fetcher (`artfetch.py survey` then `fetch`) was run afterwards
and `etl.py` again, since the read model takes cover sizes from the survey
and shows width 0 for anything unsurveyed.

The two Christmas records (`Mariah Carey / Merry Christmas (1994)`, 19 tracks
on 2 discs, and `Wham! / Last Christmas (1984)`, 4 tracks) were held by
`--skip-pool Christmas` and ingested later the same day on the user's
instruction. Mariah's went into the existing
`Christmas/Carey Mariah-Merry Christmas` folder: four old copies matched by
fingerprint and were replaced; six more (the 1994 master against the 2024
remaster, too far apart for chromaprint) were left beside the new `Disc 1`
and retired by hand to `_superseded` with `musiclib._move` (op `ingest`, so
`undo --op ingest` brings them back). Staging is empty.

### The CD+G karaoke store (12 Sep 2026)

`D:\Music\Karaoke` holds 45.9 GB of CD+G karaoke rips, 10,680 songs, mostly
Sunfly discs (SF001-373, Gold, Most Wanted, Arbiter, TeenPack), plus Quick
Hits, Rigmar and monthly packs from other producers. Each song is an MP3
backing track (128 kbps, untagged) and a `.cdg` file: the disc's raw
CD+Graphics subcode, 24-byte packets at 300 a second (7,200 bytes per second
of song), drawing a 300x216, 16-colour screen with tile, palette and preset
commands. The lyrics are pixels; the sung-so-far highlight is a colour swap
over them. A 60-line decoder in `karaoke_tidy.py`'s sibling notes rendered
frames correctly (Pink, ABBA).

**Kept out of the music library on purpose.** `scan.py` scans the pools it is
given, so the folder is never a sub-library; the karaoke code will index it
itself, like the stems store.

**Moved to `C:\Music\Karaoke` on 8 Oct 2026** to free space on D: (57 GB).
`cdg.db` stores paths relative to the store, so nothing in it changed;
`cdg.py`, `server.py`, `tools/discref.py` and `karaoke_tidy.py` use
`<music folder>\Karaoke` when it exists, else `C:\Music\Karaoke`
(`JUKEBOX_CDG` overrides both). Do not recreate `D:\Music\Karaoke`: it would win.

`library/karaoke_tidy.py` (12 Sep 2026) brought the dozen naming styles to
one: `Karaoke\<Artist>\<Artist> - <Title> [<disc code>].mp3/.cdg`. Zips
were extracted straight to their destination and deleted after a size check;
WMA and WAV backing tracks were transcoded to MP3 (`ffmpeg -q:a 2`) with the
originals moved to `_superseded\Karaoke`; songs with no settled artist went to
`_unsorted`, unpartnered files to `_orphans`, track lists and covers to
`_source`. Which side of "A - B" is the artist was settled with jukebox.db's
artist names and track titles, a disc folder's majority order, and the
artist named by a Gold or Quick Hits folder; catalogue names ("Presley,
Elvis", "Beautiful South, The") were turned round. Every action is in
`library/karaoke_tidy.log.jsonl`. The disc code is kept because about 1,500
songs exist on more than one disc.

**CD+G into the karaoke screen's timed text: thinking, not built.** The
screen wants what `align.py` produces: lines with start times and per-word
times. CD+G has no text, so it would be optical:

1. Decode the stream and watch the screen. A lyric line appears as a run of
   tiles drawn in the text colour on one or two rows; the 6x12 tile grid
   makes rows trivial to find and letters sit on a fixed baseline.
2. Segment characters by connected components in the row and recognise them
   against a glyph table. Sunfly uses one bitmap font across hundreds of
   discs, so the table can be built once from a few songs whose text is
   known (the track titles, the library's synced lyrics) and then reused;
   Tesseract on a 4x upscale is the fallback for other producers.
3. Timing comes free: the highlight sweep changes tile colours left to right
   as words are sung. The first frame a word's tiles change colour is that
   word's start, so the output is word-level, better than the LRC lines the
   lyric cache holds and equal to forced alignment.
4. Where `lyrics.db` already has synced text for the song, skip OCR: match
   the recognised lines fuzzily to the known lyric lines and take the text
   from the cache and the timing from the disc. Errors in the glyph table
   then cost nothing.

Cost is decoding 300 packets a second and a handful of frame diffs per line,
seconds per song in Python; the whole store is an overnight batch. The
payoff is that a CD+G song opens in the same screen as a split song, with
the same typography and the same word swell, and the disc's own instrumental
plays instead of a demucs backing track. The cheaper alternative, drawing
the CD+G screen on a canvas as the disc drew it, is an afternoon's work and
looks like 1990s karaoke; it could be the first step while the OCR path is
built, since the audio, the index and the picker are the same either way.

### Speaking instead of typing on the television (21 Sep 2026)

A WebView has no speech recognition: the Web Speech API
(`webkitSpeechRecognition`) is a Chrome feature, not a WebView one, and over
plain HTTP a browser would refuse it anyway as an insecure origin. So voice
search goes through the wrapper. `MainActivity` adds a JavaScript interface
named `MusicMateTV` with exactly two methods and no arguments -- `hasVoice()`
and `startVoice()` -- because the page arrives over plain HTTP on the house
network and should be given nothing worth stealing; all it can do is open
Android's own "speak now" dialog. The words come back through
`onActivityResult` and are handed to the page as `mmVoiceResult(<json>)`,
quoted through JSONObject so an apostrophe in "Don't Stop Me Now" cannot break
the call. The page shows a microphone in the search bar when the bridge is
there (hidden everywhere else, so phones and desktops are unchanged) and, on
the television, starts the d-pad ring on it rather than on the keyboard, since
not typing is the whole point. `?voice=1` draws the button without a
television, for headless checks.

The remote's own microphone key is wired too (`KEYCODE_SEARCH` and
`KEYCODE_VOICE_ASSIST`, the latter by number at 231 to leave the build's
minimum API alone), but **most Android TV remotes give that button to the
system assistant and the app never sees it**. That is why the on-screen
microphone exists: it is the route that always works. Whether the key arrives
can only be found out on the device.

### Lyric timing, and one search box for everything (20 Sep 2026)

The user: lyrics on split songs "pause differently to the words being sung";
search keeps the last words typed; discs, Christmas, Era and the collections
"are not searchable".

**Timing.** Measured over the 325 prepared songs: the screen already took 89%
of its lines from forced alignment (word count and placement reject nothing;
the 11% are lines the aligner itself doubted). The fault was in how an
aligned line was placed: `L.t = Math.min(L.t, st[0] - 0.12)` kept the EARLIER
of the LRC's time and the measured one, so an early LRC stayed early. A fifth
of lines appeared more than half a second before they were sung, 7% more than
a second, and whole files sat out by a constant (Eagles, Lyin' Eyes: +2.02 s,
spread 0.24). Now an aligned line is placed at its first sung word less a
0.25 s reading lead, and the un-aligned lines are shifted by the MEDIAN drift
of the song's confident lines. Validated by holding out every third confident
line and predicting it: typical error 0.44 s -> 0.24 s, lines over half a
second out 46% -> 28%, over a second 14% -> 8%. Carrying the drift of the
nearest confident line each side was tried and measured worse in the tail
(p95 1.05 -> 1.44) because it chases per-line noise; the median won on every
percentile. Line ends are recomputed after the shift, since a line's neighbour
is what says when it must leave the screen.

**Search.** `closeSearch(true)` clears the box and results, and every result
row passes true: picking something ends that hunt, while backing out keeps the
words. `/api/search` now merges karaoke discs from `cdg.db` (`_cdg_search`,
one row per song, the same best-read pressing the discs page shows) since they
are in their own index and were never in the read model's FTS table; the page
draws a `disc` kind that opens disc mode. Christmas, Era, Compilations and
Soundtracks WERE in the index all along and were being found -- what failed
was opening them: `resolveTrack` looked the album up in `loadAlbums()`, which
is main-pool only (2,800 of 3,480 albums), so a sub-library hit fell through
to bare playback with no album or artwork. It uses `loadAlbumsAll()` now.
`?find=<words>` opens search prefilled, for headless checks.

### Karaoke discs in the karaoke screen (12 Sep 2026)

The CD+G-to-timed-text idea above is built. `jukebox/cdg.py` (run with the
karaoke venv's python: RapidOCR, numpy, Pillow):

- `scan` indexes `D:\Music\Karaoke` into `jukebox/cdg.db` (table `song`: id =
  SHA-1 of the .cdg, artist/title/code from the tidy layout, paths, duration,
  text state, and `lib_skey` when artist+title match a library track). 10,531
  files gave 9,513 songs: about a thousand discs carry byte-identical graphics
  for the same song and fold to one id.
- `text` reads a song: decode the stream, sample the screen every 100 ms as
  packed RGB (a disc paints black as two palette indices; SF134 broke on
  indices), find horizontal bands of non-background pixels, track each band's
  life (growing while drawn, the same band while wiped, remnants of a wipe
  suppressed), and at its end work out the fill colour (the fatter-stroked of
  the colours that changed: outlined fonts change both), split words at gaps
  >= 5 px (letters gap 1-3, spaces 6-10 on every font measured), and time each
  word from the highlight sweep (start at 15% of its pixels changed, end at
  85%). Text comes from glyph bitmaps looked up in `jukebox/cdgglyphs.json`
  (learnt: a band OCR'd with one character per glyph teaches its glyphs), with
  RapidOCR recognition-only on a 3x upscale for bands with unknown glyphs. An
  I-or-l glyph is settled by the word (I-words, all-caps, position). Where the
  song matches a library track with cached lyrics, spelling and case are taken
  from there and run-together words split, timing shared by letter count. Output
  `jukebox/cdgtext/<id>.json` holds `synced`, `words` (the screen's shapes),
  and `raw` (before correction) for evaluation.
- `eval` compares the raw reading with lyrics.db for matched songs. The first
  full run (7 h) exposed glyph-table pollution: one wrong OCR line taught a
  font's letters wrong and, every glyph then "known", nothing re-checked, so
  half the store read as noise. Now a glyph needs two agreeing readings (two
  thirds of all), an assembled line under 60% dictionary words (55k words
  from lyrics.db and the track titles) is OCR'd instead, and OCR that is not
  words teaches nothing; each song's wordness is in `song.note`. After the
  re-read of the 4,773 poor songs: 9,504 of 9,513 read, median word precision
  0.90 and recall 0.92 over 1,247 matched songs, median wordness 0.955, 128
  songs still under 0.7 (non-English discs and a few hard fonts). About 3 s a
  song. `text --redo` re-reads everything; reset `text_state` to redo a subset.

Server: `/api/cdg/summary`, `/library`, `/artist?name=`, `/for-tracks?ids=`,
`/{id}/status|lyrics|words|envelope|audio.mp3|offset|cast`. The envelope is
the backing track's (decoded to WAV first; envelope.py reads PCM), computed
on first open into `cdgtext/<id>.env.json`. Lyric offsets are stored in
`kar_sync` under the key `cdg:<id>`.

Page: the Karaoke page shows a "Karaoke discs" card with the count and a
"Browse the discs" button; `#/discs` lists them by artist (the shared
`karGroups` builder, twelve artists a frame, an A-Z rail, a filter box off
the TV); an artist's own page ends with a "Karaoke discs" block of the covers
of them; `?cdg=<id>&at=<sec>` opens one headlessly. `karOpenDisc(song)` runs
the karaoke screen in disc mode: `KAR.disc` set, `KAR.tid` null, one `<audio>`
on `/api/cdg/<id>/audio.mp3`, no AudioContext, the vocal and music controls
hidden (`[hidden]` beats their `display:flex`), Sonos casting the MP3 through
`/api/cdg/<id>/cast`. Disc songs have ids `cdg:<hash>` on the page and never
enter the play queue or Now Playing: the user was explicit that the discs are
covers and must stay separate from the split songs while still being
represented; the artist-page block and the disc card are that representation.

Both karaoke pages open with a Split songs | Karaoke discs toggle in the
header (the user asked for it at the top, not a card at the foot). The
shared `karGroups` row builder must copy `disc` through: it once dropped it
and disc rows did nothing when picked.

Not done: a disc badge on library track rows (`/api/cdg/for-tracks` exists
for it), discs in search, and discs in the "Sing them all" run.

14 Sep 2026, after the user's spot check ("SOMETHINg", "BIggER", seventeen
Dancing Queens): `cdg.py` gained `polish()` (a word mixing cases beyond its
first letter is one glyph read two ways and is settled to the majority case; a
line in capitals becomes sentence case with the I-forms kept; an unknown word
one edit from a lyric word seen five or more times takes it; a low apostrophe
read as a stop or comma is restored) applied to new reads and, by `cdg.py fix`,
to the stored text in place (8,149 of 9,504 songs changed, two minutes). The
learner normalises case before teaching glyphs. The library and artist lists
return one version per song via `_cdg_best_of_each` (wordness, then a Sunfly
code, then the fuller lyric, then duration; `?all=1` for every pressing) with
the rest counted as `alts`, shown on the row as "· N more": 9,504 pressings
are 7,655 songs. Wordness was back-filled into `song.note` for the songs read
before it existed, since the choice depends on it.

Full re-read with the matured table (14 Sep 2026, chosen versions first via
`--ids chosen_ids.txt --before <epoch>`, alternates after; about 5.5 h in all):
9,504 read, median wordness 0.976, 61 songs under 0.7 (4 under 0.5), median
word precision 0.92 and recall 0.93 over 1,325 matched songs. Most bands
still go to OCR under the stricter trust rule (557k OCR against 185k
assembled), which is why a pass runs at 3-4 s a song; the table has 16,856
trusted glyphs.

The server launcher (`serve.ps1`) died three times from outside -- a
Ctrl-C-class exit (9 Sep), Windows running short of memory during a headless
Chrome render (12-14 Sep), and once unexplained (20 Sep) -- and each time
nothing brought it back until `Start-ScheduledTask MusicMate`. Fixed 21 Sep
2026: the task carries a second trigger that fires every five minutes for ever
(an EMPTY repetition duration in the task schema; `[TimeSpan]::MaxValue` is
rejected as out of range), with `MultipleInstances IgnoreNew` so a healthy
launcher is left alone and the attempt costs nothing. `RestartCount` never
helped because it covers a task that FAILS, not one killed outright. Proved by
killing both processes: back in 150 s, and at worst five minutes since the
repetition falls on five-minute boundaries. `serve.ps1` no longer deletes
`logs\STOP` at startup and refuses to run while it exists -- otherwise the
watchdog would have undone every deliberate stop a few minutes later.

The TV once showed a bare `{"detail":"Not Found"}` JSON page (12 Sep 2026,
after the toggle change; cause not found, nothing in the page navigates the
window). The server now logs 404s that a browser navigated to, with address
and user agent, in `jukebox/logs/notfound.log`; the wrapper no longer counts
a page as loaded unless it is the app (`#view` present) and treats an HTTP
error on the main frame as a miss. Force-stopping and reopening the app
clears the bad page.

### Two songs at once on the TV (10 Sep 2026)

The user's report from the night before: a song did not start, went back and
played it again and it did; opened karaoke and at some point the song and its
karaoke version were both playing at different positions; picked another song
and the old one carried on under the new one with Now Playing showing nothing
playing; left the app and the music went on behind the TV's own menu.

Three causes in `ui.html`, one in the wrapper:

1. **`karOpen` stacked.** A second press of the mic, or a song chosen from the
   Karaoke page while the screen was already open, ran `karLoad` again. It
   built a second pair of stem `<audio>` elements and overwrote `KAR.aB/aV`
   without pausing the first pair, which an element that has lost its last
   reference does not do: it plays on, invisibly, until the page dies. Now
   `karOpen` calls `karTeardown()` (the audio/timer half of `karClose`,
   factored out) when the screen is already open.
2. **A stale `karLoad` finished later.** The load awaits three fetches, the
   AudioContext resume and `play()`; a teardown or a reopen in between was
   ignored and the load still created and started stems. `KAR.gen` is bumped by
   every teardown and `karLoad` gives up after each await if it changed.
3. **A new song did not close karaoke.** `startCurrent` set the main player
   going with the stems still running. It now calls `karClose()` first when
   `KAR.on`. This is why Now Playing showed nothing: the main `<audio>` was
   paused, the sound came from orphaned stems.
4. **Background playback.** A WebView keeps playing when the activity leaves
   the foreground; `MainActivity.onPause` now calls the page's `tvSuspend()`,
   which pauses the main player and the stems when the output is this device
   and leaves a Sonos room alone. Needs the rebuilt `mm.apk` (74 KB, built
   10 Sep) installed on the TV through Downloader at `/mm.apk`.

### The Karaoke page on the TV: Could be next behind a button (10 Sep 2026)

The page took seconds to appear on the television and under a second on a
laptop. Measured: the stems folder scan is 80 ms, the queries 170 ms, but
`/api/karaoke/library` returned 1.9 MB in about a second (FastAPI's generic
encoder over 8,800 dicts, no gzip) and the page then built all 8,500
"Could be next" rows in one synchronous pass, each registered with the d-pad
observer, with a linear artist scan per group. The Ready list is 298 rows.

Now the library call returns ready, inflight and `candidates_n` (72 KB,
0.5 s); `?candidates=1` adds the list. The page shows the count with a
"Show the N songs" button; pressing it fetches the list and `mkGroups` builds
the artist blocks twelve per frame (`perFrame`), with the ring moved to the
first song on the TV. The artist lookup is a name map, not a scan. Not done
yet, and worth it if the wait still shows: gzip on API replies, and a plain
`json.dumps` `Response` for this endpoint instead of the generic encoder.

The "did not start" first press is unexplained: the play history has a row for
it (17 seconds of ticks, then a restart of the same song three minutes later),
so the server saw the stream start. The server log carries nothing at that
level. Watch for it.

## Open issues

1. **The TV layout is still "a jumble of things overlapping badly."** The
   stretched oval and cropped art are fixed; something else is not. A fresh
   photo plus a `/?diag=1` run is the fastest route. Do not theorise from a
   photograph — that produced two wrong diagnoses today.
2. **Karaoke has never been driven by a human.** Every check was headless. The
   audio sync between the two stems in particular has only been verified as code.
3. **Synced-lyric coverage grows as albums are viewed.** (1 Sep 2026) Album
   and collection track lists now carry `lyrics: yes|no|unknown` from the cache
   alone; after a list renders the client calls `/api/lyrics/warm?ids=...`
   (40 at a time, 0.2 s between lookups) and microphones appear on rows that
   turn out to have time-synced lyrics. LRCLIB was reachable again when this
   was built (~1 s per lookup including the Genius fallback on a miss). A
   library-wide background pass is still worth doing if browsing feels slow
   to fill in.
4. **The mic is on track rows and on Now Playing.** (1 Sep 2026) Row mic
   (`.trmic`) opens karaoke for that row's own track via `karPressTrack(t)`;
   `karOpen` takes the track object so it no longer assumes the song playing.
   The Now Playing mic had a bug: `karMicUpdate()` was only called from
   `karClose()`, so it never appeared until after a karaoke session. It is now
   called from both now-playing paint functions.
5. **The whole app lives in a temp scratchpad directory.** The DB is rebuildable
   so this has been tolerable, but it is a real risk. The stem store was
   deliberately placed outside it for this reason.

---

## Deferred, by explicit instruction

- Chromecast / Android TV as cast targets — "don't do it, just wondering".
- The Android TV APK. Note the TV's browser is Chrome 74, so a WebView there is
  likely the same vintage; a wrapper inherits those limits.

## Standing constraints

- Only touch files in **VariousArtists**. Nothing in
  christmas, era, tv shows.
- **Do not touch the compilations folder.**

---

## Design rules for the karaoke screen

Learned the hard way today; changing any of these will reintroduce a bug the
user already reported.

1. **Only move on real data** — the envelope, the lyric timeline, or an offline
   beat grid. A room can hear the song; motion that only pretends to follow it
   is spotted in seconds and discredits the honest parts.
2. **No scoring or pitch display, ever.** There is no microphone. It would be
   fiction and everyone would know.
3. **Words must never change rows mid-phrase.** The swell is a
   `transform: scale`, which cannot affect layout. An earlier version animated
   variable-font weight and pinned every word's width to compensate; that caused
   three separate layout failures, and measurement showed the weight axis was
   not widening the glyphs at all, so the mechanism bought nothing.
4. **The instrumental countdown is an overlay, never a screen swap.** Replacing
   the lyrics and putting them back costs the singer their place. The upcoming
   line stays on screen, unfilled.
5. **Lyric lines are persistent elements that slide between slots.** Rebuilding
   them makes the queue pop and the eye cannot follow the line away.
6. **Word fill is continuous, never a discrete pop.** Estimated timings are
   always slightly wrong; a sweep hides the error, a jump advertises it.

## D-pad navigation review (3 Sep 2026)

A usability pass over Up/Down/Left/Right/Enter/Back, driven with real key
presses in headless Chromium. Findings, the model as it now stands, and the
open items are in `docs/tv-navigation-review.md`. The validated key sequences
are a regression suite:

```
python jukebox/tools/tvnav_check.py            # 16 cases against http://127.0.0.1
python jukebox/tools/tvnav_check.py --only prefs now-bar -v
```

Rules that came out of it, each one a shipped bug: every tappable thing goes
through `reg()`; every overlay opens with `tvEnter(el)` and closes through
`tvExit()` (which restores the ring to where the overlay was opened from);
visibility is `tvVisible()` (`getClientRects`), never `offsetParent`, which
is null for a fixed element such as the now bar; inside a layer the stops are
read from the DOM because the IntersectionObserver cannot see a stop clipped
by a sheet's own scroll box; and the landing spot after a route change is
decided from the DOM, not from the observer's set, which is a frame behind.

---

## Background processes (all die with the session)

- port **8730** — the real jukebox, started before today, **needs restarting**
- port **8731** — `python -m http.server` serving the karaoke mockup
  (`<scratchpad>/mockserve`), plus the stem listening test at `/listen.html`
- port **8732** — a second `server.py` used for testing today; kill it

The mockup is also wired into `server.py` as `/karaoke-mock`, so it survives
without the temporary server.

## TV search keyboard (3 Sep 2026)

QWERTY by default, ABC as a choice (a key on the keyboard's bottom row and a
Preferences segment; `jb.kb` in localStorage). Keys are fixed-size buttons in
explicit `.kbrow` rows: the old ten-column `1fr` grid rendered six wide and
four narrow columns on the Hisense, and `aspect-ratio` is not understood
there. From the ✕, Left and Down always land on the keyboard; the
`search-close` case in `tools/tvnav_check.py` guards it.

### Direct drop into VariousArtists and Christmas (16 Oct 2026)

About 8,500 tracks (693 albums) were copied straight into VariousArtists and
815 into Christmas, bypassing `ingest.py`. The Christmas albums had already
been filed from `ToBeProcessed\_filed`, with duplicates set aside in
`_duplicates_already_released`, so Christmas was indexed only, not modified.
Tidy of the VariousArtists drop, after `musiclib.py scan` + `fingerprint`
(library.db backed up to `~\MusicMate-backups\`):
four albums already held under other names were retired track by track to
`_superseded` (op `album-dupe`; the new copies won on quality every time):
Genesis *Wind and Withering*, Pet Shop Boys *Behavior*, Talking Heads *77*,
Weezer *Blue Album Deluxe*; a second "Hot" in Missy Elliott's *Under
Construction* likewise. `foldcredit.py`: Perfect Circle -> A Perfect Circle,
Tom Petty and the Heartbreakers -> Tom Petty. `unifyartist.py`: Fleetwood
Mac, The Human League, Merle Haggard album-artist tags. Left on purpose: Lou
Reed and Metallica's *Lulu* and one Craig David guest credit (the tool would
also rewrite the per-track artist). Most fingerprint "duplicates" were
studio albums against hits sets already held, kept as always. Note
`album_dupes.py` resolves `jukebox.db` at `library/jukebox/`, a path from
before the repo move, so it no longer runs as is.
Then the System-page rescan (VariousArtists + Christmas, covers on): main
library 3,963 albums / 1,356 artists, Christmas 120 albums / 1,660 tracks.

### Cover fetch rebuilt (16 Oct 2026)

The rescan's cover step had fetched 368 of 1,393 and was asking the same
1,000 refused albums on every run. Causes, all in `library/artfetch.py`:
the search sent the raw tag ("Vitalogy (2011 Remaster)" finds nothing as an
exact phrase; the folder name was never tried); iTunes was asked only when
Deezer returned nothing at all, so Deezer offering wrong records ended the
search; one 403 switched iTunes off for the rest of the run; Deezer's quota
answer (HTTP 200 with an error body) read as "no results"; compilations and
soundtracks were searched under whoever sang track 1. And six regexes held
literal backspace characters where `\b` was meant (`incomplete.py` had one
too), so `WRONG_RECORD`, the volume check and parts of `artist_ok` had never
matched anything: "Bon Jovi" had the Greatest Hits cover, "Hysteria" a live
medley single, "American Idiot" the Broadway cast album.

Now: `album_facts` reads the majority tags and the folder (disc folders take
the parent, "Artist-Album" and "VA-" collection names are split);
`queries` tries tag and folder titles, each cleaned by `search_title`, under
the album-artist, the folder artist, "Various Artists" or the title alone
as fits; `lookup` asks Deezer exact, Deezer free text, MusicBrainz/Cover Art
Archive, then iTunes, until an answer is accepted and downloads; each source
has a pace and a cool-down (`Source`). `accept` adds the title-only rules
(exact core title via `core_title`; a soundtrack credited to its star only
when the answer calls itself a soundtrack) and refuses a "Various Artists"
answer for an album filed under one artist ("The Fame" had matched the
"Fame" film). `match_score` now accepts an extension of our title only when
the extra words read as an edition or the artist's name ("Deep Forest" had
matched "Deep Forest Delevingne Crystal Clear"). Refused albums are asked
again after 30 days, not on every rescan; `fetch --retry` asks now,
`--only <prefix>` limits it.

Audit of accepted covers against the repaired matcher: 44 replaced by a
better answer (old images in
`~\MusicMate-backups\replaced-covers-20261016`), the rest kept
because nothing better was found. Left for the user: Soundtracks/Miami Vice
TV and Compilations/Salsa Hits 2004 (cover is a near miss either way).

`fetch --retry` over the 1,025 albums under 600px that every earlier run had
refused: 462 fetched (Deezer 226, MusicBrainz 217, iTunes 19), 563 still
nothing; ~70 min. A recheck after tightening the soundtrack rule dropped one
(Bend It Like Beckham had the stage musical's cast album) and replaced one
(Gladiator: The Lyndhurst Orchestra -> Hans Zimmer). Covers in the read
model after the rebuild: albums with any art 3,884 of 3,963 main, 113/120
Christmas, 51/134 Compilations, 94/122 Soundtracks (before: 3,862, 108, 26,
49); 600px and up: 3,500 main. The next rescan asks only new albums and
those refused more than 30 days ago.

**Folder images as a last resort (17 Oct 2026).** `etl.py` now uses a cover
image saved beside the audio (`folder.jpg`, Windows Media's
`AlbumArt_{guid}_Large.jpg`; backs, inlays and `*Small` skipped) when nothing
was fetched and no file has embedded art: `art_kind='folder'`, `art_path`
pointing into the music folder, read in place. 78 collection albums gained a
cover (Compilations 51 -> 107 of 134, Soundtracks 94 -> 111, Christmas
113 -> 119), most of them 170-200 px. The 78 main-library albums still without
one are bootlegs, live tapes, "Assorted" folders and demos with no art anywhere.

## Singing along on a phone (17 Oct 2026)

The karaoke screen shows a QR code (top right) for `http://<LAN address>/?sing=1`.
A phone that opens it gets a join card naming the song on the TV; **Join** (the
tap phones need before they will play sound) puts it on the karaoke screen in
follow mode: the same lyrics, no transport, a **Backing / Lead / Full** switch,
and a **Phone earlier/later** nudge for headphones (Bluetooth runs ~0.2 s late;
kept per phone in `jb.singlat`). The room keeps playing whatever its own
switches say, so one person can sing with the voice in their ears while the
room hears only the backing. It follows song changes, pauses and seeks, and
goes back to "Waiting" when the TV leaves karaoke. A computer can click the
code; the TV gets no link so it cannot navigate away from itself.

- **Clock.** The leading screen POSTs `/api/karaoke/live` every second and at
  once on play, pause, seek and lyric nudge (`singReport`); the server stamps
  it with its own clock. The phone polls `GET /api/karaoke/live` every second
  and estimates the server-clock offset from the poll with the shortest round
  trip of the last 12 (`singClock`). Nothing is persisted; a report older than
  6 s means no song.
- **Audio.** Two persistent `<audio>` elements, unlocked by the Join tap with a
  silent clip and reused for every song. Backing/Lead/Full is `muted` on one
  or the other (iOS ignores `volume`). Opus stems where the phone plays Opus,
  else the server's MP3 renders `mix-m1-v0` (backing) and `mix-m0-v2` (voice
  alone); a phone without Opus says so (`mp3=1`) on its polls from the join
  card on, and the server renders both in the background before Join.
- **Lining up, not steering** (`singAlign` / `singStart` / `singSteer`). Two
  devices playing the same file from the same point stay within a few ms over
  a song (clocks differ by tens of ppm), so the phone lines up once and then
  leaves the audio alone: it pauses both stems, parks them `lead` (0.8 s,
  doubled whenever buffering outruns it) ahead of the room, waits until that
  stretch is buffered, and presses play when the room arrives there, less the
  device's measured start lag (`jb.singstart`; learned on the first song, 90 ms
  in headless Chromium). While running it changes nothing; only a sustained
  slip (smoothed error > 0.15 s for 1.5 s, e.g. a network stall), a jump in the
  room (> 1 s: a seek), the voice wandering > 0.1 s from the backing, or the
  singer's nudge lines up again. If a phone will not buffer while paused it
  starts anyway after 5 s and lines up once data flows.
  History (17 Oct 2026): the first version steered continuously with
  playbackRate on raw currentTime readings (which move in ~25 ms steps) --
  ~80 rate changes a second, heard as choppy audio; the second smoothed that
  but still seeked while playing when > 0.3 s out, and on a phone a seek
  stalls to buffer and lands late, so it chased itself: "super clippy and
  can't keep time" on Danceophobia. Measured now (headless Chromium, TV and
  phone): 1-20 ms in steady play, after a seek and after pause/resume, with
  no rate changes at all.
- **Backing / Lead / Full is which stems play**, not which are muted. Muting
  gave a dropout every ~5 s in Backing and Lead on the user's phone (a muted
  element's clock wanders; the voice/backing check lined up again). The
  unwanted stem is paused and the stem that plays is the lyric clock
  (`KAR.aB`); dropping a stem is seamless, adding one lines up once (~1 s).
- **The phone's own log.** Open `/?sing=1&log=1` and the phone posts a sample
  a second and every line-up with its reason (`start:`, `late:`, `paused`,
  `jump`, `slip`, `voice`, `learn`, `mix:`, `nudge`) to `/api/diag` as
  `{sing: [...]}`; `GET /api/diag` returns the last five posts, so collect it
  with `python jukebox/tools/singlog_collect.py out.jsonl` while they sing. The
  user's phone is Chrome 155 on Android 10. After a refresh it showed no
  drift line-ups at all: Full -12 ms median, Backing +17, Lead -61, and a
  start lag of 0.18-0.3 s that varies start to start, so the learning was
  damped to once a song, past 60 ms, moving 70% of the way. The dropouts the
  user heard before were almost certainly the page still running the muting
  version.
- **Testing:** never run a headless karaoke screen against the live server
  while `GET /api/karaoke/live` shows a song: the test screen takes the
  phones (it happened twice on 17 Oct 2026).
- The screen reports itself not playing while it buffers (`seeking`, or
  `readyState < 3`) and reports again on `playing`/`waiting`; before, phones
  ran ahead after a seek by however long the TV took to buffer.
- **Two screens.** The screen that opened a song last owns the phones
  (`claim` on its first report); others' reports are ignored until the owner
  has been quiet for 6 s. Before that they took turns and phones jumped.
- Playwright's WebKit on Windows cannot play media at all, so **an iPhone has
  not been tried**: that is the first thing to check on real hardware.
- `?sing=1` skips the library boot (route, session restore, cache warming):
  Home's covers held the phone's six connections and starved the clock polls,
  which made it seconds late in the first test.
- The QR code is drawn by `jukebox/qr.py` (byte mode, level M, versions 1-10),
  written here rather than added as a dependency; it matched segno module for
  module on every mask.
- CD+G discs work too, lyrics and backing only (a disc has no vocal track, so
  the switch is hidden).


## Remote navigation fixes (17 Oct 2026)

- **Lists walk in order.** Up/Down inside a `.trlist` go to the neighbouring
  `.tr` in DOM order, not the nearest by geometry: on the TV (Chrome 74, html
  `zoom` 1.5) the geometric pick skipped every other song once a long list
  scrolled (a Billboard top 100 in Era). In a two-column list Down from the
  foot of the left column continues at the top of the right; past the end of
  the list it leaves the list (never back into the other column). Not
  reproducible headless; fixed by construction.
- **The A-Z rail steps letter by letter** (skipping dim letters). Its letters
  are ~20px and the Up/Down row band is 40px, so ties went to DOM order and Up
  skipped a letter (S to U, never T). **Left from the rail** lands on the first
  thing showing in the list (the letter just picked) -- it used to look for a
  tile level with the letter and found none when the letter sat beside the gap
  between tile rows.
- **"All songs" on an artist page** is reached with Right from the end of a
  Top songs line, like a shelf's "See all" (`.trlist` joins `.strip,
  .shelfrow` in that rule). No key reached it before.
- **Each page draws into a fresh `#view`.** All pages shared one element, so a
  page still loading when another opened kept appending: Home's shelves landed
  above or below the next album (seen headless when navigating within ~2 s of
  Home; the TV is slower) and gave the d-pad stray stops. `route()` swaps in a
  new `<main id="view">`; a stale route's late output goes to the detached one,
  and its scroll/focus landing is skipped.
- **Shelves step tile by tile.** Left/Right along a `.strip`/`.shelfrow` go
  to the neighbouring tile: tiles past the shelf's edge are clipped by its
  scroller, so the IntersectionObserver never put them in `vis` and Right
  stopped at the last one showing (six on the TV). Home's Recently played
  (25 albums from the last 100 plays) and Recently added (25) were raised
  from 12 at the same time.
- `tools/tvnav_check.py` skips its `karaoke-screen` case while
  `/api/karaoke/live` shows a song (it took over the user's phone).

## Backdrops behind Now Playing and karaoke, and Cycle (17 Oct 2026)

The user asked for more interest in the background while music or karaoke
plays, and a mode that changes it on every song. Two things were in the way:
Now Playing hid the backdrop almost completely (on the television a 97% lid,
because without backdrop-filter the page's tiles leaked through the old 55%
wash), and the karaoke screen had its own three blobs that ignored the
backdrop setting. Now there is one engine.

- **The backdrop rises.** `openNP` adds `body.npon` and `karOpen`/`karOpenDisc`
  add `body.karon` (through `bdKarLift`), which lift `#gl`, `#bd2d` and
  `#glfall` to z-index 59 / 79, directly under the panel. Every style paints
  an opaque frame, so nothing underneath shows and Now Playing is a 34-74%
  vertical wash with no blur. The karaoke screen goes transparent (`#kar.bdx`),
  hides its blobs, and gains `#karscrim`, a 52% band behind the lyric rows, on
  top of the vignette it already had: the text keeps its ground whatever is
  moving. A phone following the room (`KAR.follow`) never lifts; it keeps the
  classic blobs and no canvas.
- **Ten styles plus Cycle** (`BD_STYLES`, picker in the Preferences sheet).
  Smoke, Gradient, Sleeve, Aurora, Embers, Grooves (was Grain; the id stays
  `grain` so a saved choice still works), Glow, Streams, Kaleidoscope, Pulse.
  Sleeve blurs the cover once into a 96 px tile (`bdSoft`), Aurora strokes
  each ribbon three times at falling width, Kaleidoscope mirrors the cover
  once into a 3x3 tile: no per-frame `ctx.filter` anywhere. **Cycle** is a
  rule, not a style: `bdSong(key)` runs on every song start (`startCurrent`,
  `adoptRemoteTrack`, session restore, `karOpen`, `karOpenDisc`) and, when
  `jb.backdrop` is `cycle`, hashes the song key against `BD_CYCLE` minus the
  style just shown, so the pick is deterministic per song-after-song and
  consecutive songs always differ. Gradient is left out of the pool because
  it does not move; Smoke drops out where WebGL has been demoted. A change of
  style mid-session (Cycle, or the sheet) **crossfades**: `bdApply` keeps the
  last frame of the old 2D style in `BD.fade` and draws it over the new one
  at falling alpha for 1.6 s. Into or out of Smoke there is no snapshot (a
  WebGL canvas cannot be copied after its frame), so that change still cuts.
- **Settings**, all per device: `jb.backdrop` (also `cycle`), `jb.motion`,
  `jb.karbd` = `glow` (default: the karaoke screen keeps its classic look) or
  `same` ("Behind the karaoke lyrics: Same as music"). `?bd=<style>` and
  `karbd=same` force a choice for one load.
- What moved on what, and how it was measured, is superseded by the section
  below: the first version read a WebAudio analyser that the television does
  not have.

## Backdrops that listen: the music clock and the Verse layer (17 Oct 2026, later)

The user tried the ten styles on the living-room television: "I was hoping
they would be more sound reactive, smooth, but reactive a bit. The pulse
seems to be a one time thing ... grain just scrolls to the lower right, with
no change in direction or speed based on the music playing (or not
playing)." The diagnosis: every style read `GL.energy` / `GL.bass` from
`updateEnergy`, a WebAudio analyser on the page's own `<audio>`. The TV's
music goes to a Sonos room, so the browser hears nothing, and inside the TV
app's WebView the analyser yields nothing even when it plays locally. The
styles were honest about that (they sat at their calm level), which is
exactly what the user saw. Karaoke had solved the same problem for its vocal
stem a month earlier (`envelope.py`: analyse the file once, read by
position). This pass does the same for the whole mix, then rebuilds every
style on the result.

**Server: `jukebox/musicfeat.py`, `GET /api/track/{tid}/feat`.** numpy and
ffmpeg only. Decode to 11 kHz mono, one STFT (1024-point Hann, 110-sample
hop = 100 frames a second, chunked), then:

- `loud` A-weighted power in dB against the track's own 99.5th percentile
  (45 dB floor), `low`/`mid`/`high` = 20-160 / 160-2000 / 2000-5500 Hz each
  against its own range; `flux` = half-wave-rectified change of a 40-band
  log-mel spectrum with the slow level removed. All pooled to **20 frames a
  second, quantised to bytes, shipped base64**; `onsets` = peaks of the flux
  in centiseconds.
- **Tempo and beats**: autocorrelation of the onset strength with Ellis's
  log-Gaussian prior around 110 bpm, then dynamic-programming beat tracking
  (Ellis 2007, tightness 400) at that period; `bpm` is then the median
  interval of the grid. The octave is decided from the grid itself, measured
  on five records: a grid one octave too fast has beats of alternating
  strength (`alt2` 0.5-0.6 on the wrong octave, 0.02-0.32 on the right), and
  a ballad picked at double speed has a half-tempo grid that is both more
  confident and stronger. `beatconf` (0..1: on-grid against off-grid
  strength, the autocorrelation peak, interval steadiness) is what the client
  reads before trusting the grid. `bar` assumes 4/4 and names the beat index
  that carries the most attack and bass as beat one, with its own margin.
- **Structure**: `sections` from a checkerboard-kernel novelty curve over the
  self-similarity of the log-mel spectrum at 2 frames a second (16 s kernel,
  8 s minimum gap, each boundary with the mean loudness of the section it
  starts); `arc` = loudness smoothed over ~6 s, one a second; `climax` = the
  loudest 20 s window; `balance` = each band's share of the song.

Results on the test tracks (expected tempo in brackets): Danceophobia
125.0 (125), Take on Me **84.5** (169 / 84.5: the 169 grid alternates 0.52,
so the half-time feel wins, which is what Spotify lists too), Hotel
California live 75.0 (~75; the first pick was 151), Someone Like You 67.4
(~67.5; first pick 135), Yesterday 96.8 (~97). Confidence 0.70 (Adele) to
0.90 (Duran Duran). **Cost 0.6-2.0 s a track** on this machine, scaling with
length (decode ~0.35 s, the STFT ~0.5 s, beat tracking ~0.1 s for four
minutes); **25-45 KB gzipped a track** (uniform 20 Hz streams are most of
it; the grid and the onsets are a few KB). Cached as
`jukebox/cache/feat/v1/<skey>.json.gz` (gitignored) and served as the file
lies with `Content-Encoding: gzip`, a week's cache. The first request for a
song starts one worker thread per skey (`_FEAT`) and waits up to 1.5 s for
it; past that it answers **202 `{pending, retry}`** and the page polls. A
track ffmpeg cannot decode is cached as `{error}` so it is not retried every
play. `tools/feat_batch.py` pre-analyses the library (resumable, process
pool): about 1.5 GB and six hours at four workers for 52,942 tracks. It has
NOT been run; the on-demand path plus the client's prefetch of the next song
is the default.

**Client: the music clock (`MC`, `mcPos`, `mcUpdate`).** One reading of the
music for every style, on every output. `mcPos()` is the one place that
knows where the song is: `audio.currentTime` locally; on Sonos `OUT.pos`
carried forward from `OUT.posAt` between polls (the speaker reports whole
seconds every 1.5 s, so `pollRemote` now re-anchors only when the count
turns over and the prediction is off by more than 0.35 s, or drifts clear by
1.6 s, the karaoke clock's idea); in karaoke `KAR.el`, which `karFrame`
already keeps. `bdSong(key)` calls `mcSong(key)`, which fetches the features
(cache of eight songs, `mcFetch` follows the 202) and five seconds later the
next song in the queue. `mcUpdate(now)` runs once per frame (both loops
call it; a second call in the same frame is a no-op) and derives:
`loud/low/mid/high/flux` smoothed with fast attacks and slow releases;
`beat`, a pulse shaped on the grid (50 ms rise, decay over 30% of a beat),
scaled by `beatconf` and the attack under that beat, with `beatNew` true for
one frame, `phase` 0..1 and `down` on beat one when the bar is sure;
`onset`, an impulse from the last attack; `section` and `lift` (1 at a
boundary, decaying over 2.5 s) and **`dir`**, a heading that turns 1.1-2.0
rad at every section change, eased over 4 s; `climax` eased in over 3 s and
out over 4; `mood` from the arc; **`live`** (1 playing, 0 at rest, 0.6 s /
1.8 s) and **`drift`**, music time: it advances at
`live * (0.3 + 0.7 loud) * (bpm / 112)`, plus a 0.12 breath at rest, so
every style's clock runs at the song's tempo, hurries when it is loud and
settles when it stops. Calm (`bdRate` 0.5) halves the accents as well as the
clock; Still freezes the clock and holds frames. Without features (a song
still being analysed, a disc) the vocal envelope or the analyser stand in at
`known` 0.6; nothing depends on the analyser any more. `GL.energy` /
`GL.bass` are still written (the shader reads them) but from the clock.

**The motion grammar every style shares** (the header comment above
`BD_STYLES`): the clock is `MC.drift`; the heading `MC.dir` turns at section
changes; the beat is a nudge (`MC.beat`), never a strobe; the climax lifts
the whole scene; pause brings everything to rest; behind karaoke lyrics the
accents are halved (`BD.amp` 0.55). What each style is now:

- *Smoke*: flows on music time along the heading (`uD` uniform); the beat
  pulses the zoom, loudness and climax light the caustics.
- *Gradient*: deliberately still, the lightest option; left as the one that
  does not move. The set needs it for the weakest screens.
- *Sleeve*: the cover haze in two layers sliding against each other (a far
  one, and a near one turned over that leans the way the song is heading),
  breathing with the loudness, a 2% nudge on the beat, the dark rim pulling
  back for the chorus.
- *Aurora*: one ribbon per band (bass, mids, treble), each brighter with
  its band; the wave travels at tempo, shivers on the beat, and the ribbons
  glide to new heights at a section change.
- *Embers*: motes rising at the song's pace with the wind on the heading,
  flaring in threes on attacks, lifting together on the beat; a reserve joins
  for the climax; at rest they hover and dim. One soft sprite stamped, not
  arcs.
- *Grooves* (was Grain): a record off-centre turning at tempo with a
  reflection sweeping its face; each beat sends a ring of light out along the
  grooves; the grain sparkles with the treble and drifts along the heading;
  stopped, it stops. The old version was a vignette and a noise tile sliding
  to the lower right at one speed, which is the one the user named.
- *Glow*: three pools, one per band (the bass pool swells on the beat), on
  slow orbits that turn as a whole to new corners at a section change.
- *Streams*: particles at the song's pace on a field that turns with the
  heading; attacks brighten the strokes; the climax wakes 40% more
  particles; at rest the trails settle to a still field of dots.
- *Kaleidoscope*: wedges turning at tempo, reversing through zero at a
  section change (`cos(dir)`), a 3.5% zoom on the beat, the centre pool
  lifting for the climax.
- *Pulse*: a ring on every beat of the grid, stronger on beat one and under
  a real attack, reaching the edge two beats later; with `beatconf` under
  0.35 the rings follow attacks instead; the halo breathes with the
  loudness; at rest the last rings dissolve. This is the one that "came out
  at the beginning and then nothing".

Nothing was cut. Grain was the only style with no reason to exist and it
was rebuilt rather than dropped, because a turning record is the one
metaphor a jukebox owns.

**The Verse layer: REMOVED 18 Oct 2026** (the user: "I don't like the artist
picture and words showing up randomly, let's lose that"). The code, the
Preferences row and `jb.bdwords` are gone; what follows is the record of
what it was. **The Verse layer (`WL`, `wlSong`, `wlTick`, `wlDraw`).** The user's second
ask: "pop a lyric or two on the screen ... floating through the background
twisting away, something that focuses on the chorus ... pop up pictures of
the artist/band." It is a layer over whatever style is on, drawn after the
style in `bdLoop`, only on Now Playing (`body.npon`), never in karaoke
(the screen IS the lyrics), not on phones (their Now Playing has no free
band), and switchable ("Words and faces on Now Playing"). On a song start
it fetches `/api/lyrics/{tid}`; with time-synced lines `wlMark` finds the
chorus as the lines of three words or more that come back, and the first
line of each run of them is the one that **blooms** (large italic, 10% of
the canvas height, shadowed in the accent, growing 0.88 to 1.18 and turning
on its vertical axis as it goes, 9 s). Other lines drift by small now and
then: every third line at most, one every 4.5 s, two alive at once, 7 s
each. Placement is the outer bands only (7-14% and 86-93% of the height,
alternating), clamped inside the canvas; alpha 0.17 small, 0.26 big plus a
tenth of the beat. Each phrase is rendered to a bitmap ONCE (`wlRender`)
and only the bitmap is transformed per frame. The artist's one photo is
loaded on song start, graded once into the light half of the palette
(`wlGrade`: luminance to c1, c2, white; feathered edges) and ghosts in at
88% of the height at the side at a section change or the climax
(`screen` composite, alpha 0.3 + loudness), alternating sides, 14 s a
visit, never twice in 25 s. Seeks reset the lyric cursor so nothing from the
past is read out. Under Smoke and Gradient, which have no 2D loop, the layer
rides on the same canvas set to overlay mode (`BD.overlay`: cleared each
frame, only the layer drawn).

**Measured** (headless Chromium, software raster, `Emulation.setCPUThrottlingRate`
4, viewport 1280x720 with `?tv=1`, which is the television's layout width,
so the backdrop canvas is 448x251; the earlier numbers were at 1920x1080):
Pulse 0.47 ms, Glow 0.50, Sleeve 0.73, Streams 0.74, Kaleidoscope 0.99,
Aurora 1.08, Grooves 1.3-1.4, Embers 1.8 (2.3 with the old arcs); the Verse
layer adds 0.2-0.4 ms, the clock 0.2-0.3. Analysis cost and tempo results
above. `tools/tvnav_check.py`: 14 of 17 pass; `track-row-buttons-and-sheet`,
`karaoke-page` and `karaoke-screen` fail identically with main's `ui.html`
swapped in (read-model drift since 3 Sep: album 1312 is no longer Rumours
and its rows have no mic; the karaoke page's first row does not open within
3 s), so they are not this change's; the `prefs` case was updated for the
rows added on 17 Oct. Chrome 74: `node --check` clean, no `?.`/`??`/
`replaceAll`/`finally`, no new CSS at all. Still not seen on the real
television.

Videos of every style (Danceophobia from 0:28 for the beat, Someone Like
You from 1:00 for the ballad, a paused clip, TV-mode clips, the karaoke
screen with Aurora behind Take on Me, Smoke with the Verse layer) were
recorded with Playwright at 1280x720; they live outside the repo, in the
session scratchpad, and are listed in the report for this pass.

Open: the Sonos clock is a prediction between whole-second polls, so on a
room a beat can land a few hundred milliseconds off (the tempo is right, the
phase is roughly right); the cast stream's own latency is unknown and on top
of that. Lyrics for a song without a cached lookup cost a network fetch on
song start (the lyrics pane did the same, only when open). The face grading
uses the palette as it stands when the photo arrives.

## Backdrops on the television: the NaN that blanked them, and the hit (18 Oct 2026)

First night on the TV, the user: Streams "doesn't brighten on hits", Aurora
"just dark", Embers "pops a bit but not related to the beat", "glow is just
a white screen", Pulse "seems to be nothing" -- "the sound reactions don't
seem to work". Three causes, found in this order:

- **A NaN that stuck for the whole session.** `MC.lastE` was never
  initialised. While a song is still being analysed (the first time it is
  ever played) `mcUpdate` falls back to the live analyser and computes
  `low - MC.lastE`: NaN on the first frame, and the exponential smoothing
  carries NaN forward for good, so `MC.beat` stayed NaN even after the
  features arrived. Every colour built from it threw in `addColorStop`
  mid-frame; a style that had already switched to `lighter` left it on, and
  each frame's background fill then ADDED to the last, so the screen climbed
  to white (Glow). Others lost their geometry and sat dark. Reproduced
  headless with any never-analysed track (`?tv=1&bd=glow&np=<tid>`, check
  `MC.beat`). Fixed three ways: `lastE: 0`; the smoother `sm` treats a
  non-finite value as 0; and `bdLoop` resets `globalCompositeOperation` and
  `globalAlpha` in a `finally`, so no throw can latch a drawing mode.
- **Colours drawn in near-black.** The palette is built for the shader's
  dark smoke; its middle colour is often about (0.12, 0.10, 0.11), and
  Aurora drew two of its three ribbons in it. `bdLitPal`/`bdLit` give the 2D
  styles a lit accent (HSL lightness 0.56-0.70, hue kept) and a lit second
  colour (the middle colour lightened if it has any saturation, else the
  accent deepened and turned slightly toward red). A first try turned the
  hue the other way and went yellow-green on an orange cover.
- **Reactions too small to see.** The styles did correlate with the music,
  but the canvas's mean brightness moved 0.5-6.7 levels of 255 over four
  seconds. `MC.kick`, "the hit", jumps on each beat of the grid (x1.3 on the
  downbeat) and on each strong attack and decays in 0.18 s; every style
  shows it plainly (ribbons jump and flare, pools swell, rings are a wide
  band under a sharp line, the ember swarm surges and a handful flare per
  beat, the record's ripple, the kaleidoscope's zoom and lifted centre).
  **Streams** keeps its trails on their own transparent layer
  (`BD.flow.trail`) and draws it a second time, added, in proportion to the
  hit. Drawing the main canvas over itself instead lifted the ground as well
  and compounded to white in two seconds; do not do that.
  Glow composites with `screen`, not `lighter`, so overlapping pools cannot
  pass white.

Measured with the scratch harness (TV mode, 1920x1080, 240 frames of the
canvas reduced to 32x18): mean brightness and its range over 4 s, and the
correlation of the mean with `MC.beat`/`MC.onset`. Before -> after on the
same song: Aurora range 1.1 -> 27, Glow 6.7 -> 16, Embers 0.9 -> 10,
Pulse 3.4 -> 12, Streams 2 -> 9 (thin strokes; the hit frames are plainly
brighter by eye), onset correlation 0.4-0.8 throughout; no page errors on
eight never-analysed songs.

**The TV reports what it sees.** `bdBeacon` posts the style, output,
position, whether the features loaded, the `MC` values, the lit palette and
a 64x36 thumbnail of the canvas to `/api/diag` (`kind: 'bd'`) every 20 s,
on a TV (or `?bdlog=1`), for an hour after load. `GET /api/diag` returns the
last five (in memory only). Look there before guessing about the TV again.

## Ridges and Ridges 3D (18 Oct 2026)

The user sent the Unknown Pleasures cover: "a series of lines ... that move
like a spectrum analyser ... both a still and a moving/rotating version ...
rotating in a 3d plane". Two styles, one function (`bdRidges(g, w, h, now,
turn)`):

- **Data.** `musicfeat.py` VERSION 2 adds `spec`: the analysis's 40 log-mel
  bands paired to 20 (`specn`), pooled to 10 a second (`spechz`), each band
  in dB against its own 98th percentile over 36 dB, blended 60/40 with the
  song-wide level over 50 dB so bands all move but a bass-heavy record still
  looks it. 70-78 KB gzipped per song (40 bands was 113 KB). The bump moved
  the cache to `cache/feat/v2/`; v1 was deleted (it rebuilds on play).
  The client stretches it (`((v - .3) / .7) ^ 1.5`) so quiet bands lie flat.
- **Drawing.** Each line is one spectrum frame (`bdRidgeRow`, cached per frame
  index so a line keeps its shape as it travels back): low notes left, high
  right, inside a central plateau (`exp(-((x-.5)/.23)^4)`) so the edges lie
  flat, with a fixed jagged texture per frame and point. 44 lines on a TV
  (56 elsewhere) = 4.4 s of history; the stack scrolls continuously by the
  fractional frame, the newest line fades in at the front and the oldest out
  at the back. Hidden lines: back to front, each line fills the strip
  between it and its own baseline with the ground before stroking. The ink is
  the lit accent 65% toward white. The hit lifts and brightens the newest
  five lines. Before the analysis arrives, rows are made from the three
  bands and frozen per frame.
- **3D** projects the same field (x +-1.7, depth 2.2) through a camera whose
  yaw swings `sin(drift * .12) * .6` plus a lean from `MC.dir` (sections),
  pitch `.5 + sin(drift * .07) * .12`, so a swing is about 50 s of music time
  and it holds still when the music stops. Painter's order by line index
  holds for yaw within about +-50 degrees; a full orbit would need sorting.
- **Resolution.** Fine lines show a low-resolution canvas, so `bdSize` gives
  the Ridges styles 0.5 on a TV (0.35 for the soft styles) and 0.75 elsewhere.
- **Cost** at a 4x CPU throttle: median 2.5-4.9 ms, p95 7-12 ms a frame:
  the dearest style, heavier than the others' 0.5-1.8 ms. Watch the TV
  beacon's frame rate if the TV stutters; fewer lines (`L`) is the lever.
- Samples sent to the user: 22 s of each on Love Will Tear Us Apart with the
  song's audio (Playwright video, offset by wall clock, muxed with ffmpeg).
- `tvnav_check` `artists-rail` failed once in each of two full runs on
  18 Oct and passed alone every time (step 5, Left from a rail letter):
  intermittent, unrelated to the backdrops, not yet chased.

## Ridges Colour and Spectrum (18 Oct 2026)

Ridges Heat, Prism and Orbit were tried and shipped the same afternoon
(a633a61). The user: "the fast moving one is pretty jumpy, but the other two
are good ... use the album or song art for the color palette ... pull 3 from
this, one is rotating and color, one is rotating and B&W and the third is
stationary and B&W". Taken as: Orbit (full turns, shoved by each hit) was
the jumpy one; Prism's faster swing was fine. So the family is now three,
in `RIDGE_VARS`: Ridges (flat, mono), Ridges 3D (slow swing, mono) and
**Ridges Colour** (`ridgescolor`: Prism's swing, `sin(drift * .36)`, inked
with the cover's colours laid across the spectrum). A device that had saved
one of the retired ids is moved to `ridgescolor` when the page loads.

- **Art colours.** `artColours(d)` runs beside the palette extraction in
  `paletteFromUrl` and sets `GL.art`: the cover's pixels binned into 12 hues,
  weighted by chroma squared, the strongest bins taken (at least a tenth of
  the top one, never two neighbouring bins, at most four), each lifted to a
  lightness that reads on the dark ground, ordered by hue. A grey cover gives
  null (`applyDuo` clears it too) and the style uses the lit accent and
  second colour. Love Will Tear Us Apart's sleeve is grey, so the sample was
  recorded on Foo Fighters, Making A Fire (red, blue, violet).
- **Spectrum** (`leds`): the 90s LED bar meter the user asked for. 20 bars
  from `spec`, eased between its frames, mapped `(v - .6) / .35` (spec sits
  high: per-band median about .7, 90th percentile about .85), 16 segments,
  green to 60%, yellow to 85%, red above. Meter ballistics: instant rise, a
  fall of 2.4 full scales a second, peaks held 0.55 s then dropping faster as
  they go. The hit adds up to .3 to the bass third, as a kick drum throws the
  low bars. Unlit segments show faintly, lit ones carry an additive halo.
  The trail: ten times a second (wall clock, so on pause the bars fall and
  the trail carries them off) the levels are kept, 14 deep on a TV; each
  older reading is drawn as the top segment of every bar only, rising toward
  the upper fifth of the screen and shrinking a little as it fades, so the
  past reads as glowing caps drifting up behind the meter.
- **Cost** at a 4x throttle on 960x540: Ridges 3D 2.5 ms median / 7.9 p95,
  Ridges Colour 4.8 / 12.4 (a gradient per line), Spectrum 1.1 / 3.8.
  Spectrum gets the sharp canvas scale (0.5 on a TV) with the Ridges.
- `tvnav_check` prefs: 15 tiles now (five rows).

## The 3D spectrum family: Tunnel, Synthwave, Skyline, Planet, Helix (18 Oct 2026)

Asked "any additional ones using the spectrum data ... particularly anything
3D and moving?", five were proposed and the user said "lets do them all".
All are Canvas 2D (no WebGL), share `bdSpecRow(f, idx)` (a 0..1 row per
spectrum frame, `(v - .5) / .45`, cached per frame in `BD.scache`; the three
live bands with a hash stand in before the analysis arrives, and for the
picker thumbnails, `BD.thumb`), `bdBand(row, u)` (smooth read across bands),
`bdArtCols()` (the cover's colours, `GL.art`, else the lit pair) and
`bdSpecPos()`.

- **The future on screen.** Tunnel, Synthwave and Helix lay out the coming
  music (the analysis is offline, so it is known) and bring each reading to
  the camera, or the middle for Helix, exactly as it plays. Skyline and
  Planet show the last seconds.
- **Tunnel**: a ring per reading, bass along the floor and treble overhead
  (`u = (1 - sin a) / 2`, so left and right mirror), pinched in where loud;
  30 rings on a TV at 0.3 apart; the tube bends with distance and rolls on
  music time; six rails join the rings. Rings nearer than z 0.3 are dropped
  and the far end fades to nothing (the first version clotted there).
- **Synthwave**: wireframe land, a flat road and spectrum mountains either
  side (bass nearest the road, taller toward the flanks), rows filled with
  the ground far to near so near hills hide far ones, rungs between rows,
  a striped sun in the cover's colours behind a horizon at 0.44 h.
- **Skyline**: 20 bands by 8 rows (TV; 13 elsewhere) of towers, one row a
  fifth of a second, the camera orbiting slowly (`drift * .1`). Painter's
  order by box depth; the two sides that face the camera are picked from
  the camera's world position, not by testing every face. It is the dearest
  style: 7.6 ms median at a 4x throttle even after that rewrite (13 ms
  first; fills and canvas path calls, not the script, are most of it), so
  it uses the soft canvas scale (0.35 on a TV). Fewer rows is the lever.
- **Planet**: a meridian per reading over 6.4 s, bass at the south pole,
  raised and swung sideways where loud (radial bumps alone only showed at
  the rim), the globe turning so the newest is laid down front left;
  back meridians dim, then a body disc, then front ones additive; a halo
  that breathes with loudness and the hit; slow stars.
- **Helix**: first built looking down the corkscrew's axis, which was a
  mess of huge near lines; rebuilt side-on as a DNA strand: rungs across a
  ribbon that turns about the x axis, music entering from the right,
  crossing the middle as it plays, fading left; two rails; the spin eased
  toward `(.3 + .4 * rate) * live + kick * .8` over 0.6 s so the hits
  quicken it without a jerk. The user: "dna is great".
- **Cost** at 4x on the TV-sized canvas: Tunnel 2.3 / 7.8 p95, Synthwave
  2.3 / 9.3, Planet 5.2 / 12.4, Helix 3.6 / 11.1, Skyline 7.6 (above).
- `tvnav_check` prefs: 20 tiles, seven rows (`until` max 10).
- Samples of all five on Making A Fire sent to the user.

### First feedback on the five (19 Oct 2026)

- **The music clock was jittery.** `audio.currentTime` steps unevenly from
  frame to frame, so anything scrolling with the song juddered; the user saw
  it as Synthwave "glitches a little". `mcPos()` now runs the local clock on
  the wall clock at the playback rate, eased toward the audio's time over
  0.5 s and snapped on a seek (over 0.3 s out), a pause, or a gap over 0.5 s
  (`MC.clk`). The Sonos path already extrapolated. Every style benefits.
- **Synthwave** also lifted a row on the beat by its place in the list
  (`j`), so the lift jumped from row to row as they moved; it is keyed to
  `idx - fp` now. New rows rise flat out of the horizon over six rows.
- **Skyline**: the back rows sink into the floor and darken to it over the
  last 55% of their age (smoothstep) instead of vanishing; the front row
  grows from nothing. Rows back to 9 on a TV.
- **Planet**: a meridian every 0.2 s, so a turn takes 12.8 s (was 6.4);
  the camera's elevation swings `.32 +- .26` rad over about 140 s of music
  time.
- **Helix**: the camera circles part way round the strand (`yaw .48 sin`)
  and rises and dips (`tilt .12 +- .24`), both slow; smaller field of view
  so the near end stays on screen.

### Synthwave Cover (19 Oct 2026)

"album art somehow incorporated into the synthwave, perhaps ... into the
sun". A second tile, `synthart` (`bdSynth(..., cover)`); plain Synthwave is
unchanged. `bdSunArt(cols)` makes the sun once per cover and palette
(keyed by `BD.imgId` and the art colours) on a 256 px canvas: the cover
clipped to a disc, a `soft-light` wash from the first art colour (lightened)
at the top to the last at the bottom, a light `screen` lift, and a hot rim.
The sun stands higher (centre half a radius above the horizon) so most of
the picture shows, and six bands drift slowly down through its lower part,
thickening as they go. The hit adds the sun to itself additively, a flash.
No cover (a disc, art still loading) draws the plain gradient sun.
A grey sleeve gives a cool moonlit version in the palette's accent.
- 19 Oct, after the samples: the cover version on Love Will Tear Us Apart
  "glitches and seems to go backward a bit". A watcher on `mcPos()` during a
  re-recording found no backward step, stall or frame gap over 60 ms, so it
  was not the clock. Most likely a wagon-wheel effect: the land advanced one
  row per spectrum frame (a tenth of a second), so on the road, where rows
  look alike, a frame-rate dip moved them about half a gap a frame and the
  eye read it as backward. Both Synthwaves now take a row every fifth of a
  second (`bdSpecRow(f, idx * 2)`, the clock halved), half the speed, and
  the land shows 6.8 s ahead.
- The camera is no longer fixed (the user: up and down and left and right,
  "slightly and slowly", with the sun and horizon still the vanishing
  point): it drifts across the road (`camX .28 sin(drift * .037)`), rises
  and sinks (`camH .5 +- .1`), and pans and tilts a little (the vanishing
  point moves by up to 5% of the width and 2.5% of the height), each on its
  own slow period, so no two passes look the same.

## Currents (19 Oct 2026)

From a photo of Tame Impala's Currents sleeve: "a ball being dragged
through the sound ripples". `bdCurrents`: 66 parallel diagonal lines on a TV
(96 elsewhere) at 0.62 rad, each drawn twice (a dark edge offset down and
right, then the light line, the album's lit accent 55% to white) so they
read as ridges. A chrome ball (radial gradient, a shadow, the trail colour
caught underneath) wanders slowly about the middle on music time.

- **Parting**: each line's across position is pushed out from the ball by
  up to 1.35 Rb, the push dying fast ahead (`exp(-al^2 / 1.7 Rb^2)`) and
  slowly behind (`45 Rb^2`), leaving a groove up the way the ball came, with
  a trail in the cover's colours fading along 7 Rb of it.
- **Ripples**: ahead and to the sides only (a weight on `al / r`). A point
  at distance r from the ball is displaced by `A sin(k (r - c t) + swirl)`,
  with c = 0.12 h a second, so rings travel outward; A comes from the
  spectrum at time `t - r / c`, interpolated between frames, the band by
  angle (bass straight ahead, treble at the sides), plus the hit near the
  ball. The swirl term (`1.6 sin(lat / 5 sp + t * .4)`) bends the rings
  into the sleeve's wobbles.
- **Cost**: only lines within reach of the ball are sampled (every 0.6 line
  gaps on a TV), and behind the ball only the groove's lines; 5.4 ms median
  at a 4x throttle (8.6 before the TV line count and step were eased).

### Currents in 3D, Aurora as curtains, a Smoke size guard (19 Oct 2026)

- **Currents** feedback: "the camera needs to revolve a bit around the
  ripples ... the lines on either side should look like they are flowing
  like water past the chrome ball ... with the beat the ripples should get
  more intense". Rebuilt in world units (ball radius 1 at the origin, lines
  along z) under a perspective camera that always looks at the ball:
  yaw `1.85 + .7 sin(drift * .05) + .15 sin(dir)`, pitch `.98 +- .14`.
  Water: a swell runs back along the lines (`sin(kw (al + flow))`, `flow`
  accumulating with loudness), strongest beside the ball and in its wake;
  the groove sways; dashed glints (`setLineDash`, offset by flow) stream
  along. Beat: the hit is kept in a short history (`BD.ckh`), so each ring
  carries the beat of the moment it left the ball, and the whole field gains
  `1 + K * .5`. Ripples also lift in height so they read in perspective.
  The spectrum and beat at each ring distance are tabled once a frame
  (40 distances x 5 bands). Cost 6.2 ms median at a 4x throttle (TV spacing
  .38, coarse straight runs); the first 3D cut was 10.
- **Aurora** feedback: "distinct lines not a blending of the areas". Now
  curtains of vertical rays, brightest just above a soft lower edge and
  fading up into the other palette colour, brighter where the curtain folds
  edge-on, worked out pixel by pixel into a sixth-size ImageData and
  stretched over the screen (the stretch is the blur). Drawing the rays as
  ~300 small drawImage/fillRect calls cost 11-14 ms; the pixel loop is 1.2.
- **Smoke** on the TV sat squeezed into the top-left corner (user report,
  not reproduced headless). `drawGL` now resizes the buffer if it no longer
  matches the window and sets the viewport every frame, and the TV beacon
  also reports while Smoke runs (`win`, and `gl` with viewport, box, scale,
  tier), so the next report shows the TV's numbers if it recurs.
