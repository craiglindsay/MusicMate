# What's new in MusicMate

Newest first. Tap a version in Preferences to come back here any time.

## 2.1 — 3 November 2026

### New
- **Three new Christmas backdrops**, joining the ones that come up on Christmas songs:
  - **Snow Globe.** A cottage, a lamp post and a snowman under glass, on a turned wooden base. The snow swirls in the water for as long as the music plays (faster when it's loud, a pulse on every beat, a shake at the song's big moments) and takes most of a minute to settle when it stops.
  - **Sleigh Ride.** A horse-drawn sleigh crossing a snowy forest by night, the trees sliding past at the song's pace and the horse trotting in time with the beat.
  - **Northern Lights.** Green and violet curtains waving over snowy mountains, mirrored in a frozen lake.
- **Five new backdrops made of particles**, all under Preferences → Backdrop, and in Cycle's rotation:
  - **Cymatics.** Sand on a vibrating plate. The grains settle into geometric figures that follow the sound (simple for bass-heavy music, intricate for bright music), jump on the hits, and scatter and re-form when the song moves to a new section.
  - **Stardust.** The album cover made of thousands of specks. Quiet passages hold the picture; every beat blows it apart and it drifts back; the song's big moments let the whole cover fly and re-form.
  - **Murmuration.** A flock of starlings wheeling against a dusk sky in the cover's colours. It tightens and speeds up as the music gets louder, surges on the beat, and splits in two and rejoins when the song changes section.
  - **Fireflies.** A meadow at night. The fireflies blink at random while the music is soft and fall into step with the beat as it builds. The calm one.
  - **Fireworks.** Shells timed to burst on the beat, bigger when the music is louder, with a finale at the song's climax.
- **Comet backdrop.** A new backdrop made from a print spotted in a hotel: a ball woven of little coral scribbles with long threads of light streaming off it. The ball turns and swells with the music, and the music runs out along the threads as waves. Pick it under Preferences → Backdrop; Cycle uses it too.
- **Free up space on your phone.** Downloads lists everything kept on the phone, least played first, with how much room each takes and when you last played it. Remove one, everything not played for a month, or all of it. When the phone fills up, MusicMate says so and the downloads carry on once there is room again.

- **Playback check.** Preferences has a Playback check screen: which address and stream size this device uses, a test of its connection to MusicMate (how quickly it answers, download speed, how fast a song starts), and a log of what happened to the last songs, from the phone and from the server side.
- **Cancel a request.** Every album still being looked for or downloaded has a Cancel request button; anything already downloading for it is dropped, and the album shows as missing again so it can be asked for later.
- **Requests say when downloads have stopped.** The top of Requests shows whether downloads are running (how many, how much is left, how fast) or why they have stopped, such as the download drive running low on space, with a Resume downloads button.

### Improved
- **Christmas backdrops touched up.** Snowy Forest: snow now builds along each branch, from a few mounds to a full load, instead of appearing in blocks. Tree: it now stands in a room, with a window onto a snowy night, a rug and presents. Baubles: more of them, in mixed sizes and finishes (glitter, banded, frosted, mirrored), hanging from pine boughs and reflecting the coloured lights. Frost: the ice now grows on a window with a warm, lit room out of focus behind it. Starfield: a low curtain of northern lights and a snowy horizon.
- **Better snow in the Christmas backdrops.** In Snowy Forest, Snowdrift and Village the snow now has depth: fine specks far off, soft flakes nearer, and a few big out-of-focus ones right in front. Each flake swings and drifts on its own, the snow comes down in wandering sheets, and a few flakes catch the moonlight. In the forest a bough that lets go now drops a puff of powder that hangs in the air. Snowdrift gained stars, a moon and far hills.
- **Cover backdrops stay on the song that's playing.** Sleeve, Kaleidoscope, Synthwave Cover and Stardust used to switch to the cover of whatever album you were browsing; now they keep the playing song's cover. With nothing playing they still follow what you're looking at.
- **The DJ starts straight away** with a quick hello, instead of a 20-second wait while it wrote its opening. The personal opening (what the station is about, who you just heard) comes after the first few songs.
- **Songs start more reliably on a slow or laggy connection.** Fetching songs ahead now waits until the song that's on is safely buffered, so the two no longer fight over a weak connection.
- **Phones stream at the download size.** Over Tailscale a phone now plays the smaller copy (Standard unless you chose another size under Downloads), without the cover art that songs carry at the front, so a song starts after a few kilobytes instead of most of a megabyte and uses about half the data. Choose Original under Downloads to stream the files as they are.
- **Play after a pause works on a patchy connection.** If the phone dropped the paused song's connection, Play now fetches it again and carries on from where it stopped.
- **Music picks itself up when you come back to the app** if the phone paused the sound while MusicMate was in the background. If a phone refuses to start a song on its own, it now says to tap play instead of sitting at 0:00.
- **Music keeps playing on a weak signal.** A phone playing on itself (over Tailscale) now fetches the song that's on and the next three in full, ahead of time, so a patchy connection doesn't stop them. If the signal drops in the middle of a song, it picks up where it stopped as soon as it can.
- **A song that gets stuck starting is asked for again.** Away from home a song's connection could hang for good while a fresh one would go straight through, so a song that has nothing to play after 8 seconds is now fetched again (twice) on its own. Fetching ahead lets go of a song you skipped past, or one that has stopped arriving, so it doesn't crowd the song that's playing.
- **A song that can't be reached is skipped** to the next one already on your phone, with a note saying so, instead of leaving you in silence.
- **Skipping ahead starts faster.** When a song starts, MusicMate gets the next five ready at home, so skipping past the ones already on your phone doesn't wait several seconds while a song is prepared.
- **MusicMate updates itself.** When a newer version is out, the app loads it on its own while no music is playing on that device and you're not using it. If music is playing, a button at the top offers the update instead (the music stops until you press Play).

## 2.0 — 2 November 2026

### New
- **Who's listening.** A profile button at the top: pick yourself, add a new person, or choose Everyone. Each person gets their own favourites, recently played, most played, smart playlists and podcasts. Everyone sees the whole house.
- **Podcasts.** Search for any show, follow it, and pick up where you left off. Each person has their own shows and resume points, and can see everyone else's. The newest episode of every followed show is kept at home; the rest stream. A phone can keep the newest episodes for offline listening. Skip back 15, forward 30, and play faster or slower.
- **Wrapped.** Your year in music as a show: minutes listened, top artists, songs and albums, genres, decades, the hours you listen, your listening personality and more, hosted by the DJ, who says your name. Open it from the profile button any time and look back at earlier years. From 1 December to 7 January it gets a banner on Home.
- **Wall panel.** A tablet on the wall can be a MusicMate panel for its room: it plays on that room's speaker, shows Now Playing or a clock when left alone, goes black at night and wakes with a tap, and keeps itself up to date.
- **Home Assistant on the wall panel.** A house button shows your Home Assistant dashboard full screen, with the music bar still at the bottom and a Music button to go back. Left alone for three minutes, the panel returns to the music.
- **Start music from Home Assistant.** Dashboard buttons and automations can play an artist, album, playlist, genre or DJ station in any room, several rooms together, or everywhere, and pause, skip or set the volume.
- **Rooms.** One screen for the whole house: what every room is playing, play or pause each, set each room's own volume, take control of any room, play what you're hearing everywhere, or pause everywhere. On the wall panel it's in the top menu; on other screens, from the speaker list.
- **Speaker volume from the bottom bar.** While a Sonos room is playing, a speaker button on the bottom bar sets its volume.
- **DJ on any room.** The DJ button has a ▾ to choose the room before the station starts.

### Improved
- **The DJ keeps going on a speaker when your phone sleeps.** The server tops up the station itself.
- **Plays count the way Last.fm counts them:** half the song or four minutes, whichever comes first. A song skipped after a few seconds no longer counts, and the whole history was recounted that way.
- **Setting a room's volume** in the Play-in list works as soon as you tick the room, before you press Play.
- **The speaker list** looks again by itself if it comes back empty.

## 1.9 — 1 November 2026

### New
- **Christmas backdrops.** Christmas songs get their own scenes behind Now Playing: Snowy Forest, Fireplace with stockings, Fairy Lights, swinging Baubles, a snowy village and more, each a little different every time. Turn them off in Preferences.
- **Small Now Playing.** The backdrop fills the screen, with the cover small in the corner and the play bar underneath.

### Improved
- **Eras use the year an album first came out,** not the year of a remaster or reissue. About 470 albums moved to their right decade (AC/DC is back in the 70s and 80s).
- **The DJ sounds like a late-night DJ:** shorter, casual, names the artists rather than every song, and drops the odd bit of trivia.

## 1.8 — 31 October 2026

### New
- **The DJ.** Start a station from an artist, song, album, genre or era: sets of well-known songs with a spoken link between them, voiced by a choice of DJs.
- **Ask for an album.** Artist pages show what is missing from the library, and a Get button requests it. New music is filed into the library by itself every ten minutes.
- **Away from home.** MusicMate works on your phone anywhere through Tailscale, privately.
- **Downloads on your phone.** Keep albums, favourites and playlists on the phone to play with no signal, at a size you choose.

### Improved
- **Songs keep the same identity** when the library is rebuilt, so queues, favourites and karaoke survive new music arriving.
- **Artist pages:** "not in your library" goes last, below top songs, similar artists and karaoke.

## 1.7 — 17 to 19 October 2026

### New
- **Backdrops that move to the music** behind Now Playing and karaoke: Glow, Streams, Kaleidoscope, Pulse, Ridges, LED Spectrum, Tunnel, Synthwave (with the album cover as the sun), Skyline, Planet, Helix, Currents, Aurora and Smoke, or Cycle through them.
- **Sing along on your phone.** Scan the QR code on the karaoke screen and the phone plays the backing track or the lead vocal in time with the TV.

### Improved
- **Album covers:** the cover finder was rebuilt to search three sources and match more carefully.
- **Remote control:** lists and the A-Z rail step one at a time; Home shelves hold 25 albums.

## 1.6 — 5 October 2026

### New
- **MusicMate goes public** at github.com/craiglindsay/MusicMate, with a README and a configurable music folder so others can run it.

## 1.5 — 20 to 25 September 2026

### New
- **Voice search on the TV,** using the remote's microphone.

### Improved
- **Karaoke word timing** is much more accurate: it finds where the lyrics really start and lines each block up to the singing.
- **Lyric lines** sit where the singing is.
- **The server restarts itself** if it is ever stopped.
- **The TV app** recovers after an outage and its address search no longer skips.

## 1.4 — 8 to 14 September 2026

### New
- **Karaoke discs.** About 10,500 CD+G karaoke songs, with their words read from the disc graphics, alongside the split songs. A toggle switches between the two.
- **Billboard Year-End charts,** 2007 to 2025.
- **New albums filed from a drop folder,** including a big batch of Christmas records.

### Fixed
- Karaoke never plays a song twice; the karaoke filter hides what it should.

## 1.3 — 3 to 4 September 2026

### New
- **The TV app.** An Android TV app that opens MusicMate full screen, with remote-control navigation throughout.
- **Per-song covers on compilations,** shown in Now Playing, the bar, the queue and karaoke.
- **Karaoke on phones,** from the bottom bar.
- **"Sing them all":** a shuffled run through every prepared karaoke song.

### Improved
- The karaoke page is grouped by artist, with a filter and an A-Z rail.
- The TV search keyboard is QWERTY by default.

## 1.2 — 2 September 2026

### New
- **Preferences** behind the gear: backdrops, motion, how soon Now Playing takes over, display size.
- **System page:** server health, rescan the library, fetch missing covers, backups.
- **Sleep timer, crossfade,** and drag to reorder playlists.
- **Libraries.** Christmas, Compilations, Soundtracks and the other collections each get their own card on Home.
- **Favourites** has its own page with songs, albums and artists.
- **Recently added** shelf on Home.
- **Karaoke words light up as they are sung,** timed from the recording itself.

### Improved
- MusicMate starts by itself with the PC, and backs up your favourites and history every day.

## 1.1 — 1 September 2026

### New
- **Karaoke** on track rows and its own page, following whichever speaker is playing.

### Fixed
- Wrong album covers replaced, artist names unified, and duplicate albums folded together.

## 1.0 — 28 August 2026

The first MusicMate: the music library tidied and the jukebox built on top of it.
- Browse by artist, album, genre and era; search everything.
- Play on this device or any Sonos room, or several rooms together.
- Now Playing with lyrics, a queue, shuffle and repeat.
- Favourites, playlists, smart playlists and play history.
- Artist radio and similar artists.
- Karaoke: any song split into vocals and backing, with the words on screen.
- Works on the living-room TV, phones and computers.
