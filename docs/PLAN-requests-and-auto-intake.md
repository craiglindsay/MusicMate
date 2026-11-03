# Plan: request missing music from MusicMate, and auto-update the library

Status: **built 2026-10-30** (stable ids, Part A, Part B; see docs/HANDOFF.md, "Stable ids, auto-intake and requests"). Written 2026-10-30 as a proposal; the decisions below were made the same day.
Author: the Claude Code session that runs the download pipeline (`D:\Projects\UNPoller`).

## Background: where new music comes from

`D:\Music` gets fed by a Usenet download pipeline that lives in `D:\Projects\UNPoller` (read its `CLAUDE.md`):

```
MusicBrainz release-group IDs (MBIDs)
   └─► Lidarr :8686 ──search──► Prowlarr :9696 ──► NZBgeek (the only indexer)
          └─► SABnzbd :8080 downloads to %USERPROFILE%\Downloads\complete\music
                 └─► Lidarr imports into %USERPROFILE%\Music\<Artist>\<Album (Year)>
                        └─► batch_convert.py      FLAC → AAC 256 M4A (iPod-safe), deletes the FLAC
                        └─► release_completed.py  complete albums → %USERPROFILE%\ToBeProcessed\<Artist>\<Album (Year)>
                                                    and an entry in UNPoller\music-pipeline\processed_albums.txt (the ledger)
                               └─► MusicMate library\ingest.py --apply   → D:\Music\<Pool>\...
                                      └─► MusicMate rescan (POST /api/admin/rescan)
```

As of 2026-10-29 the hand-off into `D:\Music` goes through **MusicMate's own `library/ingest.py`**. From 10-08 to 10-29 the pipeline used its own `file_albums.py`, which skipped `library.db`. That has been fixed, and a full rescan picked everything up. Today each step is run by hand from a Claude session.

`ToBeProcessed` now holds only `Artist\Album (Year)` folders. Its old holding folders moved to `~\ToBeProcessed_holding\`, because `ingest.py` walks every folder in staging.

There are two goals:

1. **Part A, auto-intake:** the library updates itself when new albums arrive, with no manual ingest and no manual rescan.
2. **Part B, requests:** from MusicMate, find artists, albums or songs the household doesn't have, and press a button to send them to the download queue. Then show the request's progress until it's playable.

---

## Part A: auto-intake (build first)

### What
A background loop that runs every 30 minutes:

1. `UNPoller\music-pipeline\batch_convert.py "%USERPROFILE%\Music" --workers 4`
2. `UNPoller\music-pipeline\release_completed.py`
3. `MusicMate\library\ingest.py --apply`
4. If step 3 moved anything: the existing rescan (`musiclib.py scan VariousArtists Christmas …`, `fingerprint`, `artfetch.py survey/fetch`, `etl.py`).

### Where it should live
Put it in the **MusicMate server** as a background thread, using the same subprocess-and-log pattern as `_rescan_run` in `jukebox/server.py`. The server already runs all the time under the `MusicMate` scheduled task with its five-minute watchdog. The pipeline's own `pipeline_driver.py` only runs while a list is being processed, but requests (Part B) can arrive at any time.

There is an alternative if you'd rather keep the server out of the pipeline: a separate scheduled task that calls the scripts, then `POST /api/admin/rescan`. Either way works. What matters is that something always-on owns the loop.

### Rules
- **Don't rescan while something is playing.** Track ids change on every `etl.py` rebuild (only about 1 in 7 survive; see CLAUDE.md), so a rebuild mid-song can break Now Playing and the queue. Wait for idle on every output (browser and Sonos), or run at a fixed quiet hour. Then show a "New music added: reload" toast instead of the current blocking Reload prompt.
- **Leave albums that need a person in staging.** That means albums `ingest.py` blocks (AcoustID disagreement over 30%) and albums the pipeline couldn't import. The usual cases are Various Artists and soundtracks, deluxe editions with no matching MusicBrainz edition (e.g. Pentatonix *Christmas in the City* 26-track), and partial albums. List them as **"Needs attention"** on the System page with the reason from `ingestreport.txt`.
- **Disk-space guard.** Stop ingesting when D: free space falls below a threshold (suggest 3 GB) and say so on the System page. D: had about 1.7 GB free on 2026-10-30; moving Christmas, Compilations and Soundtracks to `C:\Music` (junctions in `D:\Music`, see CLAUDE.md) brought it to 33.7 GB, with C: at 22.6 GB. The guard must check the volume each album actually lands on: VariousArtists is on D:, the other three pools are on C:.
- **One job at a time.** Use a lock shared with the manual Rescan button, and with demucs if CPU matters. `release_completed.py` already uses a lock file.
- **Log** to `jukebox/logs/`, with the tail shown on the System page next to the rescan log.

### Known mismatch to settle
`ingest.py` names Christmas folders `Christmas\Artist - Album`, but the existing folders use `Artist-Album` (no spaces; the owner chose that on 2026-10-16). Pick one and change `ingest.py` or rename the existing folders. Also, the `CHRISTMAS` regex in `ingest.py` has `noel` but not `noël`.

---

## Part B: find missing music and request it

### B1. Missing albums on artist pages (cheapest, most useful)
On an artist page, add a **"Missing albums"** shelf: the artist's studio albums from MusicBrainz that aren't in the library.

- **Discography:** look up the MusicBrainz release groups for the artist (primary type Album, no secondary types). Cache them in `library/mb_cache.db`, which already exists.
- **Owned check** (any one is enough):
  - normalised artist + album matches `library.db`; reuse `merge.norm_artist` / `merge.norm_album_safe`;
  - the release-group MBID is in `UNPoller\music-pipeline\processed_albums.txt` (format: `<mbid>  Artist - Album  |  path`);
  - Lidarr already has the album monitored or downloading (then show it as "Requested" rather than missing).
- Each missing album gets a **Get** button.

### B2. "Not in your library" in search
When searching, add a second results section from a MusicBrainz search:

- **Artists not in the library:** open a light artist view showing their studio albums, each with a **Get** button.
- **Songs:** resolve the recording to the **original studio album** it first appeared on (earliest release group, type Album with no secondary types; skip compilations and live albums). Show "Get *Rumours* (contains 'Dreams')". The pipeline works in whole albums, and so does the library's structure.

Respect MusicBrainz's 1 request/second limit with a proper User-Agent, and cache everything.

### B3. The button: `POST /api/request`
Body: `{rgid, artist, album}`. The server stores the request and calls a **new pipeline script** as a subprocess:

```
python D:\Projects\UNPoller\music-pipeline\request_album.py <rgid>
```

`request_album.py` would be built on the UNPoller side, so that duplicate checking and the ledger stay in one place. It packages what is done by hand today:

1. Refuse if the album is in the ledger, already on disk, or already monitored or queued (`dedupe_list.py` logic).
2. Add it to Lidarr: create the artist if new (`monitorNewItems: none`, metadata profile "All Types", quality profile "M4A"), monitor only that album, then search. This is what `batch_add.py` already does.
3. Re-apply monitoring after Lidarr's post-add refresh, which unmonitors new albums (`enforce_monitoring.py`).
4. If Lidarr finds 0 releases, search Prowlarr directly and report the candidates. **Never auto-grab a fuzzy match.** Past bulk override grabs picked singles, live albums and wrong editions. Those go to "Needs attention" for a person.
5. Print JSON: `{status, lidarrAlbumId, message}`.

### B4. Requests page and status
Add a `request` table (in `state.db`, or a separate `requests.db` to keep `state.db` purely user data). Statuses:

| Status | How it's known |
|---|---|
| Requested | row created |
| Searching / Not available | Lidarr album has no queue item and nothing on disk; "will grab automatically if posted" (Lidarr RSS keeps watching) |
| Downloading n% | Lidarr `GET /api/v1/queue` (match on albumId) |
| Converting / Releasing | files in `%USERPROFILE%\Music\…`, or the album in `ToBeProcessed` |
| Needs attention | blocked by ingest, needs manual import, or 0 releases with candidates to review |
| In your library | rgid in the ledger **and** the album present in `jukebox.db` after a rescan; link to the album |

Add a nav item or Home shelf called "Requests". Show a toast when a request lands in the library. API keys come from `C:\ProgramData\Lidarr\config.xml` and `C:\ProgramData\Prowlarr\config.xml` (`<ApiKey>`); never hardcode them.

### UI constraints
The TV runs **Chrome 74**: no `?.`, `??`, `replaceAll`, `inset`, `clamp()`, `aspect-ratio`, `:has()`, and so on (see CLAUDE.md). The buttons must be reachable with the D-pad.

---

## Decisions needed from the owner

1. **Who can request?** MusicMate has no logins, so anyone on the LAN could request. Options: free for all, a daily cap, or an approval step on the System page.
2. **Songs:** request the whole album that contains the song (recommended), or skip song requests.
3. **Rescan timing:** at most every 30 minutes when idle, or nightly.
4. **Storage on D:**, which must be sorted out before Part A runs unattended.
5. **Christmas folder naming:** `Artist-Album` or `Artist - Album`.

## Suggested phases

| Phase | Scope | Rough effort |
|---|---|---|
| 1 | Part A: auto-intake loop, idle-aware rescan, space guard, Needs attention list | ½ day |
| 2 | B1 + B3: Missing albums shelf on artist pages, `/api/request`, `request_album.py` (UNPoller side) | 1 day |
| 3 | B2: "Not in your library" search (artists, songs → album) | ½ day |
| 4 | B4: Requests page, status tracking, arrival toast | ½ day |

Phases 1 and 2 cover what the owner asked for: a button to request, and a library that updates itself.

## Pointers
- Pipeline docs: `D:\Projects\UNPoller\CLAUDE.md` (Lidarr config, the monitoring quirks, manual-import rules, **never `replaceExistingFiles: true`**).
- Pipeline scripts: `D:\Projects\UNPoller\music-pipeline\` (`batch_add.py`, `enforce_monitoring.py`, `dedupe_list.py`, `batch_convert.py`, `release_completed.py`, `import_folder.py`).
- Ledger: `D:\Projects\UNPoller\music-pipeline\processed_albums.txt`. It's authoritative for "already have it"; never delete it.
- MusicMate: `library/ingest.py` (staging → library), `jukebox/server.py` `_rescan_run` / `/api/admin/rescan` (the pattern to reuse), `docs/HANDOFF.md`.
