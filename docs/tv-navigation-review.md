# TV navigation review (D-pad), 3 September 2026

A usability pass over Up, Down, Left, Right, Enter and Back in `jukebox/ui.html`,
thought through as a music app used from a sofa: browse to something, play it,
glance at the bar, open Now Playing, change the room, sing. Every finding below
was reproduced with real key presses in headless Chromium (Playwright) at
1280x800 and, for the sheets, at the television's 1280x720. The cases that
passed after the fixes are encoded in `jukebox/tools/tvnav_check.py`; run it
after any change to the navigation code.

## The navigation model, as reviewed

- **Stops.** Anything a finger can tap is registered with `reg()`, which marks
  it `.f` and hands it to an IntersectionObserver. The candidate set for a
  page is the observer's "near the viewport" set, so a five-thousand-tile grid
  costs nothing per key press. Inside a layer (a sheet, Now Playing, search,
  the karaoke screen) the candidates are read straight from the DOM instead;
  a layer holds a few dozen stops and the observer cannot see stops clipped
  by a sheet's own scroll box.
- **Layers.** The topmost open layer owns the arrows: sheet, then karaoke,
  then search, then Now Playing, then the page. Opening a layer remembers the
  stop the ring was on; closing it (Back, a row's own action, or a tap on the
  backdrop) puts the ring back there. Only if that stop is gone does the ring
  fall back to the current section's pill.
- **Left/Right** move along the line the ring is on. A track row and the now
  bar are rows with inner stops: Right walks the row's mic and + (or the
  bar's speaker, previous, play, next), Left comes back to the row. At the
  right edge of a shelf Right goes to its "See all"; at the right edge of a
  page with the A-Z rail Right goes to the letter level with the ring.
- **Up/Down** find the nearest row above or below, then the first item in it:
  the leftmost tile in a shelf, the item in the same column elsewhere.
  Headings, "See all" and the rail are never landed on vertically. Inside the
  rail Up/Down step letter to letter and scroll the list. Anything whose
  vertical span crosses the ring's centre line is beside us, not above or
  below, and is skipped. On the karaoke page songs step to songs and artist
  pictures to pictures; Left and Right cross between the two columns.
- **Enter** clicks; held for 850 ms it toggles the favourite of whatever
  carries a `data-fav` key. Enter on a text box gives it the keyboard; Up/Down
  leave it again.
- **Back** peels layers in order (karaoke, sheet, search, Now Playing) before
  stepping back in history. After a step back the ring returns to the tile
  that was opened (`focusMem`, keyed by `data-fav`), else to the section pill.
- **Forward navigation** lands on the section pill for a root page (Home,
  Artists, Albums, Karaoke, a library) so Down enters the content, and on the
  Play button of a detail page (album, artist, genre, decade).
- **PageUp/PageDown** scroll a screenful and re-anchor the ring to the middle
  of the screen in the same column.

## Fixed in this pass

Critical (an action could not be reached or the ring was lost):

1. **Sheets were not navigable.** The + sheet (Play next / Add to queue / Add
   to playlist), the playlist picker and the output ("Play in") sheet had
   rows that were never registered, and the candidate set was not scoped to
   an open sheet, so the arrows kept moving the page underneath it. Rows,
   the room list, the volume slider, the rescan and Apply buttons are stops
   now; a sheet is a layer; the output sheet opens on the room in use.
2. **Preferences stops below the fold could not be reached.** The sheet
   scrolls inside itself when taller than the screen, and the observer never
   reports a stop clipped by that box; at 720 px the lower backdrop tiles, the
   idle and size segments and the two links were unreachable. Layers now read
   their stops from the DOM.
3. **The now bar itself was never a stop.** `offsetParent` is null for a
   `position:fixed` element, so the visibility test dropped the bar: Down from
   the page landed on its speaker button, and Enter could not open Now
   Playing from the bar. The visibility test is `getClientRects()` now, and
   the bar is a row: Down lands on it, Right walks its buttons.
4. **The ring was lost after a forward step.** The landing spot was read from
   the observer's set in the frame after render, before it had seen the new
   page: the ring went to the Home pill on every page, or to nothing, and the
   next Down then jumped to the rail's first letter. The landing is decided
   from the DOM: section pill for a root page, Play for a detail page.
5. **Search and karaoke opened without a ring.** Same cause as 4; the first
   arrow now steps into any open layer the ring is not in, search lands on
   the first key of the on-screen keyboard, and Right from inside a layer no
   longer escapes to the page's rail.

Should fix (wrong place, recoverable):

6. **Closing a layer sent the ring to the section pill,** not to the row or
   + button it was opened from. Layers keep a small stack of origins now.
7. **A ring on a hidden element stayed live.** After a sheet closed by its
   own row, the next Enter clicked the hidden row again ("Play next" fired
   twice). Enter and the arrows ignore a stop that is no longer rendered.
8. **Karaoke page: Down from the first song went to the artist picture,** a
   tall element beside it, instead of the next song; Down from the Karaoke
   pill landed on "Sing them all" rather than the artist column (user's ask).
   Both fixed by the "beside" rule and a direct hop to the first artist.
9. **Back from an artist opened off the karaoke page** returned to the pill,
   not the artist's block, because the block had no `data-fav` key. It has
   one now (and hold-Enter favourites the artist, as on an artist tile).
10. **Volume from the remote.** With a Sonos room selected, Left/Right on
    the volume slider (Now Playing, output sheet) nudge the room by 4.

## Open items

Should fix:

- **Now Playing at 720 px.** The second action row (favourite, album,
  crossfade, sleep, mic) sits at y = 731, below a 720-px screen, and Now
  Playing does not scroll. The ring reaches it but the viewer cannot see it.
  This is the same layout problem as the header/cover overlap the user
  reported; it needs the `/?diag=1` geometry from the set.
- **Queue rows in Now Playing** cannot be removed with the remote; the ✕ is
  not a stop. A row's Enter plays it. Suggest Right on a queue row → ✕.
- **New playlist** uses `prompt()`, which a WebView answers with null unless
  the app implements `onJsPrompt`; on the TV the button will do nothing.
- **Enter on the seek bar** does nothing (the bar is pointer-driven); Left/
  Right seek 10 s. Enter could toggle play, as on the karaoke timeline.

Consider:

- Down from a pill lands in the nearest column of a chip row ("Newest" on
  Albums) rather than the first chip; consistent with the grid rule, but a
  leftmost rule for chip rows would read more predictably.
- The "Refetch cover" admin button sits in the album action row and is the
  first stop Down from Play; a sofa user has no use for it.
- No key jumps straight to Now Playing; Enter on the bar is the only way.
  Media keys work (play/pause, next, previous). An "Info" key mapping would
  suit a remote.
- The rail is reached only with Right from the rightmost column, which is
  right for a grid but undiscoverable; a one-line hint on first TV use would
  help.

## Needs a remote to confirm

- The Hisense remote's Back reaches the page as `backAction()` through the
  WebView (`MainActivity.onKeyUp`); the layering order above assumes that.
- Whether the set's browser reports the A-Z rail letters as intersecting
  (the rail is fixed; letters are children, so they should).
- Long-press Enter (favourite) depends on key auto-repeat, which some
  remotes do not send. If it never fires, favouriting from the sofa needs a
  visible control.
- Volume nudges on the slider assume the remote's Left/Right arrive as
  ArrowLeft/ArrowRight while a Sonos room is selected.

## Verdict

The model is sound: line-locked Left/Right, nearest-row Up/Down with
leftmost-in-shelves, explicit layer ownership and Back that peels layers
before history is the right shape for a TV music app, and it survived every
scenario once the plumbing bugs above were fixed. The defects were almost all
in the plumbing rather than the rules: which elements counted as stops, which
layer owned the arrows, and reading a set the observer had not filled yet.
Keep new stops going through `reg()`, open every overlay through `tvEnter`
and close it through `tvExit`, and run `tvnav_check.py`.
