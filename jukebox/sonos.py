"""Sonos discovery and transport control over local UPnP.

No cloud, no account: speakers are found with an SSDP multicast search and driven
with SOAP over HTTP on port 1400. That matters because the whole point is the TV
telling a speaker to fetch audio straight from this server -- the audio never passes
through the browser, and nothing leaves the house.

The one thing this cannot do is hand Sonos a localhost URL: the speaker fetches the
stream itself, so every URI it is given must use the server's LAN address.
"""
import re, html, socket, time, threading, urllib.request, urllib.parse
from xml.etree import ElementTree as ET

SSDP_ADDR, SSDP_PORT = '239.255.255.250', 1900
SONOS_PORT = 1400
NS = {'s': 'http://schemas.xmlsoap.org/soap/envelope/'}

SEARCH = ('M-SEARCH * HTTP/1.1\r\n'
          'HOST: 239.255.255.250:1900\r\n'
          'MAN: "ssdp:discover"\r\n'
          'MX: 2\r\n'
          'ST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n\r\n')

def _local_ips():
    """Every local address, because a machine on both wifi and ethernet must
    multicast from the interface the speakers are actually on."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80)); ips.add(s.getsockname()[0]); s.close()
    except Exception:
        pass
    ips.discard('127.0.0.1')
    return sorted(ips) or ['0.0.0.0']

def discover(timeout=4.0):
    """Find Sonos players on the LAN. Returns [{ip, name, model, uuid}]."""
    found = {}
    for local in _local_ips():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            try: s.bind((local, 0))
            except OSError: pass
            s.settimeout(0.6)
            for _ in range(3):                     # UDP: ask more than once
                try: s.sendto(SEARCH.encode(), (SSDP_ADDR, SSDP_PORT))
                except OSError: break
            end = time.time() + timeout
            while time.time() < end:
                try:
                    data, addr = s.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                txt = data.decode('utf-8', 'ignore')
                if 'Sonos' not in txt and 'ZonePlayer' not in txt:
                    continue
                found.setdefault(addr[0], {'ip': addr[0]})
            s.close()
        except Exception:
            continue
    out = []
    for ip in found:
        info = describe(ip)
        if info:
            out.append(info)
    return sorted(out, key=lambda d: d.get('name', ''))

def describe(ip, timeout=4):
    try:
        with urllib.request.urlopen(f'http://{ip}:{SONOS_PORT}/xml/device_description.xml',
                                    timeout=timeout) as r:
            xml = r.read().decode('utf-8', 'ignore')
    except Exception:
        return None
    def tag(t):
        m = re.search(rf'<{t}>(.*?)</{t}>', xml, re.S)
        # room names arrive XML-escaped: "Luna&apos;s Room"
        return html.unescape(m.group(1).strip()) if m else None
    name = tag('roomName') or tag('friendlyName') or ip
    return {'ip': ip, 'name': name, 'model': tag('modelName'),
            'uuid': (tag('UDN') or '').replace('uuid:', '')}

# ---------------------------------------------------------------- SOAP
def soap(ip, service, action, body='', path=None, timeout=8):
    paths = {
        'AVTransport': '/MediaRenderer/AVTransport/Control',
        'RenderingControl': '/MediaRenderer/RenderingControl/Control',
        'GroupRenderingControl': '/MediaRenderer/GroupRenderingControl/Control',
        'ContentDirectory': '/MediaServer/ContentDirectory/Control',
        'ZoneGroupTopology': '/ZoneGroupTopology/Control',
    }
    ctl = path or paths[service]
    urn = (f'urn:schemas-upnp-org:service:{service}:1' if service != 'ZoneGroupTopology'
           else 'urn:upnp-org:serviceId:ZoneGroupTopology')
    if service == 'ZoneGroupTopology':
        urn = 'urn:schemas-upnp-org:service:ZoneGroupTopology:1'
    env = ('<?xml version="1.0"?>'
           '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
           f'<u:{action} xmlns:u="{urn}">{body}</u:{action}>'
           '</s:Body></s:Envelope>')
    req = urllib.request.Request(
        f'http://{ip}:{SONOS_PORT}{ctl}', data=env.encode(),
        headers={'Content-Type': 'text/xml; charset="utf-8"',
                 'SOAPACTION': f'"{urn}#{action}"'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'ignore')

def _val(xml, tag):
    m = re.search(rf'<{tag}>(.*?)</{tag}>', xml or '', re.S)
    return m.group(1) if m else None

def esc(s):
    return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;') \
                    .replace('"', '&quot;')

def hms(sec):
    sec = int(sec or 0)
    return f'{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}'

def didl(url, title, artist, album, art_url=None, duration=None, mime='audio/mpeg'):
    """Sonos shows nothing useful in its own apps without DIDL-Lite metadata.

    The <res> element is not optional decoration: without it the speaker reports a
    duration of 0, so no progress bar anywhere can show how far through a track is.
    """
    res = (f'&lt;res protocolInfo="http-get:*:{mime}:*"'
           + (f' duration="{hms(duration)}"' if duration else '')
           + f'&gt;{esc(url)}&lt;/res&gt;')
    inner = (f'&lt;item id="1" parentID="-1" restricted="1"&gt;'
             + res +
             f'&lt;dc:title&gt;{esc(title)}&lt;/dc:title&gt;'
             f'&lt;dc:creator&gt;{esc(artist)}&lt;/dc:creator&gt;'
             f'&lt;upnp:album&gt;{esc(album)}&lt;/upnp:album&gt;'
             + (f'&lt;upnp:albumArtURI&gt;{esc(art_url)}&lt;/upnp:albumArtURI&gt;'
                if art_url else '')
             + '&lt;upnp:class&gt;object.item.audioItem.musicTrack&lt;/upnp:class&gt;'
             '&lt;/item&gt;')
    return ('&lt;DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"&gt;'
            + inner + '&lt;/DIDL-Lite&gt;')

# ---------------------------------------------------------------- transport
def play_url(ip, url, title='', artist='', album='', art_url=None,
             duration=None, mime='audio/mpeg'):
    meta = didl(url, title, artist, album, art_url, duration, mime)
    soap(ip, 'AVTransport', 'SetAVTransportURI',
         f'<InstanceID>0</InstanceID><CurrentURI>{esc(url)}</CurrentURI>'
         f'<CurrentURIMetaData>{meta}</CurrentURIMetaData>')
    soap(ip, 'AVTransport', 'Play', '<InstanceID>0</InstanceID><Speed>1</Speed>')
    return True

def enqueue(ip, url, title='', artist='', album='', art_url=None, next_=False,
            duration=None, mime='audio/mpeg'):
    meta = didl(url, title, artist, album, art_url, duration, mime)
    return soap(ip, 'AVTransport', 'AddURIToQueue',
                f'<InstanceID>0</InstanceID><EnqueuedURI>{esc(url)}</EnqueuedURI>'
                f'<EnqueuedURIMetaData>{meta}</EnqueuedURIMetaData>'
                f'<DesiredFirstTrackNumberEnqueued>0</DesiredFirstTrackNumberEnqueued>'
                f'<EnqueueAsNext>{1 if next_ else 0}</EnqueueAsNext>')

def clear_queue(ip):
    return soap(ip, 'AVTransport', 'RemoveAllTracksFromQueue', '<InstanceID>0</InstanceID>')

def play_queue(ip, uuid, tracks, start=0):
    """Load a real Sonos queue and play from it.

    Pointing SetAVTransportURI at a single stream plays that one file with no queue
    behind it, so Next and Previous have nothing to move to and fail outright. The
    transport has to be aimed at the speaker's own queue (x-rincon-queue) instead.
    """
    clear_queue(ip)
    for t in tracks:
        enqueue(ip, t['url'], t.get('title', ''), t.get('artist', ''),
                t.get('album', ''), t.get('art'), duration=t.get('duration'),
                mime=t.get('mime', 'audio/mpeg'))
    soap(ip, 'AVTransport', 'SetAVTransportURI',
         f'<InstanceID>0</InstanceID>'
         f'<CurrentURI>x-rincon-queue:{uuid}#0</CurrentURI>'
         f'<CurrentURIMetaData></CurrentURIMetaData>')
    if start:
        soap(ip, 'AVTransport', 'Seek',
             f'<InstanceID>0</InstanceID><Unit>TRACK_NR</Unit>'
             f'<Target>{int(start) + 1}</Target>')
    soap(ip, 'AVTransport', 'Play', '<InstanceID>0</InstanceID><Speed>1</Speed>')
    return True

def simple(ip, action):
    return soap(ip, 'AVTransport', action, '<InstanceID>0</InstanceID>'
                + ('<Speed>1</Speed>' if action == 'Play' else ''))

def transport_info(ip):
    x = soap(ip, 'AVTransport', 'GetTransportInfo', '<InstanceID>0</InstanceID>')
    return {'state': _val(x, 'CurrentTransportState')}

def _didl_field(didl, tag):
    """One DIDL element's text.

    The tag must end at whitespace or '>': matching <upnp:album[^>]*> also
    matches <upnp:albumArtURI>, which silently returns the art URL plus
    everything up to the real </upnp:album> as the album name."""
    pat = '<' + re.escape(tag) + r'(?:\s[^>]*)?>(.*?)</' + re.escape(tag) + '>'
    m = re.search(pat, didl or '', re.S)
    return html.unescape(m.group(1)).strip() if m else None

def track_meta(x):
    """What the speaker says it is playing, whatever the source.

    Sonos returns DIDL for the current track even when it came from Spotify,
    radio or a line-in -- so a controller can display a room it did not start.
    Radio has no dc:creator; the artist/title pair rides in r:streamContent."""
    didl = html.unescape(_val(x, 'TrackMetaData') or '')
    if not didl or didl == 'NOT_IMPLEMENTED':
        return None
    title = _didl_field(didl, 'dc:title')
    artist = _didl_field(didl, 'dc:creator') or _didl_field(didl, 'r:albumArtist')
    album = _didl_field(didl, 'upnp:album')
    art = _didl_field(didl, 'upnp:albumArtURI')
    stream = _didl_field(didl, 'r:streamContent')
    if stream and not artist:
        # radio packs "Artist - Title" into one field
        parts = [p.strip() for p in stream.split(' - ', 1)]
        if len(parts) == 2:
            artist, title = parts[0], parts[1] or title
        else:
            artist = stream
    if not (title or artist):
        return None
    return {'title': title, 'artist': artist, 'album': album, 'art': art}

def position_info(ip):
    x = soap(ip, 'AVTransport', 'GetPositionInfo', '<InstanceID>0</InstanceID>')
    def secs(t):
        try:
            p = [int(v) for v in (t or '0:00:00').split(':')]
            return p[0] * 3600 + p[1] * 60 + p[2]
        except Exception:
            return 0
    try:
        trk = int(_val(x, 'Track') or 0)
    except Exception:
        trk = 0
    return {'position': secs(_val(x, 'RelTime')), 'duration': secs(_val(x, 'TrackDuration')),
            'uri': _val(x, 'TrackURI'), 'track': trk, 'meta': track_meta(x)}

def queue(ip, limit=600):
    """The speaker's own queue, read back as a list of stream URLs.

    This is what lets a reloaded browser tab work out what is playing without
    having stored anything: the queue is already on the speaker, and every entry
    is a URL this server handed it. It also covers playback someone started from
    the Sonos app, which no amount of local bookkeeping could know about."""
    out, start = [], 0
    while len(out) < limit:
        x = soap(ip, 'ContentDirectory', 'Browse',
                 '<ObjectID>Q:0</ObjectID>'
                 '<BrowseFlag>BrowseDirectChildren</BrowseFlag>'
                 '<Filter>*</Filter>'
                 f'<StartingIndex>{start}</StartingIndex>'
                 '<RequestedCount>100</RequestedCount>'
                 '<SortCriteria></SortCriteria>')
        didl = html.unescape(_val(x, 'Result') or '')
        uris = re.findall(r'<res[^>]*>(.*?)</res>', didl, re.S)
        if not uris:
            break
        out.extend(html.unescape(u).strip() for u in uris)
        try:
            total = int(_val(x, 'TotalMatches') or 0)
        except Exception:
            total = 0
        start += len(uris)
        if start >= total:
            break
    return out[:limit]

PLAY_MODES = {
    ('off', False): 'NORMAL',
    ('all', False): 'REPEAT_ALL',
    ('one', False): 'REPEAT_ONE',
    ('off', True): 'SHUFFLE_NOREPEAT',
    ('all', True): 'SHUFFLE',
    ('one', True): 'SHUFFLE_REPEAT_ONE',
}


def set_play_mode(ip, repeat='off', shuffle=False):
    """Let the speaker do shuffle and repeat itself.

    The queue lives on the speaker, so re-ordering it from here would fight the
    thing that actually owns it. Sonos expresses both settings as one enum."""
    mode = PLAY_MODES.get((repeat, bool(shuffle)), 'NORMAL')
    soap(ip, 'AVTransport', 'SetPlayMode',
         f'<InstanceID>0</InstanceID><NewPlayMode>{mode}</NewPlayMode>')
    return mode


def seek(ip, seconds):
    """Jump to a position in the current track.

    This is what makes handing playback from one output to another feel like a
    handoff rather than a restart: the new target is told where the old one had
    got to."""
    soap(ip, 'AVTransport', 'Seek',
         f'<InstanceID>0</InstanceID><Unit>REL_TIME</Unit>'
         f'<Target>{hms(max(0, int(seconds)))}</Target>')
    return int(seconds)


def volume(ip, level=None):
    if level is None:
        x = soap(ip, 'RenderingControl', 'GetVolume',
                 '<InstanceID>0</InstanceID><Channel>Master</Channel>')
        return int(_val(x, 'CurrentVolume') or 0)
    level = max(0, min(100, int(level)))
    soap(ip, 'RenderingControl', 'SetVolume',
         f'<InstanceID>0</InstanceID><Channel>Master</Channel>'
         f'<DesiredVolume>{level}</DesiredVolume>')
    return level

def relative_volume(ip, delta):
    """Nudge by a step, letting the speaker do the arithmetic.

    Read-modify-write loses races against the Sonos app or another phone doing
    the same thing; SetRelativeVolume is atomic on the device and returns where
    it landed. A stereo pair also tracks its own channel trim this way."""
    x = soap(ip, 'RenderingControl', 'SetRelativeVolume',
             f'<InstanceID>0</InstanceID><Channel>Master</Channel>'
             f'<Adjustment>{int(delta)}</Adjustment>')
    try:
        return int(_val(x, 'NewVolume'))
    except Exception:
        return volume(ip)

def group_volume(ip, level=None, delta=None):
    """Volume for a whole group, applied to the coordinator.

    When rooms are grouped, RenderingControl only moves the one speaker; the
    group fader is a separate service that scales every member proportionally
    and keeps their relative balance. Falls back to the single-device control
    on the older S1 player, which does not implement it."""
    try:
        if delta is not None:
            x = soap(ip, 'GroupRenderingControl', 'SetRelativeGroupVolume',
                     f'<InstanceID>0</InstanceID><Adjustment>{int(delta)}</Adjustment>')
            return int(_val(x, 'NewVolume'))
        if level is None:
            x = soap(ip, 'GroupRenderingControl', 'GetGroupVolume',
                     '<InstanceID>0</InstanceID>')
            return int(_val(x, 'CurrentVolume') or 0)
        level = max(0, min(100, int(level)))
        soap(ip, 'GroupRenderingControl', 'SetGroupVolume',
             f'<InstanceID>0</InstanceID><DesiredVolume>{level}</DesiredVolume>')
        return level
    except Exception:
        if delta is not None:
            return relative_volume(ip, delta)
        return volume(ip, level)

def mute(ip, on=None):
    if on is None:
        x = soap(ip, 'RenderingControl', 'GetMute',
                 '<InstanceID>0</InstanceID><Channel>Master</Channel>')
        return (_val(x, 'CurrentMute') or '0') == '1'
    soap(ip, 'RenderingControl', 'SetMute',
         f'<InstanceID>0</InstanceID><Channel>Master</Channel>'
         f'<DesiredMute>{1 if on else 0}</DesiredMute>')
    return bool(on)

# ---------------------------------------------------------------- grouping
def zone_groups(ip):
    """Which rooms are bonded together, and which one is the coordinator.

    Grouping matters for control: commands must go to the group's coordinator, not
    to whichever speaker the user happened to tap.
    """
    try:
        x = soap(ip, 'ZoneGroupTopology', 'GetZoneGroupState', '')
    except Exception:
        return []
    raw = _val(x, 'ZoneGroupState') or ''
    raw = (raw.replace('&lt;', '<').replace('&gt;', '>')
              .replace('&quot;', '"').replace('&amp;', '&'))
    out = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    for grp in root.iter('ZoneGroup'):
        members = []
        for m in grp.findall('ZoneGroupMember'):
            loc = m.get('Location') or ''
            mip = re.search(r'http://([\d.]+):', loc)
            members.append({'name': html.unescape(m.get('ZoneName') or ''),
                            'uuid': m.get('UUID'),
                            'ip': mip.group(1) if mip else None,
                            'invisible': m.get('Invisible') == '1'})
        vis = [m for m in members if not m['invisible']]
        if not vis:
            continue
        coord = grp.get('Coordinator')
        out.append({'coordinator': coord,
                    'coordinator_ip': next((m['ip'] for m in members
                                            if m['uuid'] == coord), None),
                    'members': vis,
                    'name': ' + '.join(m['name'] for m in vis)})
    return out

def invisible(ip):
    """Bonded satellites in the topology this player reports: uuid -> info.

    A stereo pair is two players but one room; the non-coordinating half is
    marked Invisible and must never be sent audio. When the pair's coordinator
    is off the network (asleep, unplugged, out of Wi-Fi range) the room vanishes
    from the topology entirely while the satellite still answers discovery --
    which is how "Sonos Moves" came to be listed and to refuse every queue
    (1 Sep 2026). `partner_present` says whether the other half is there."""
    try:
        x = soap(ip, 'ZoneGroupTopology', 'GetZoneGroupState', '')
    except Exception:
        return {}
    raw = _val(x, 'ZoneGroupState') or ''
    raw = (raw.replace('&lt;', '<').replace('&gt;', '>')
              .replace('&quot;', '"').replace('&amp;', '&'))
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    present = {m.get('UUID') for m in root.iter('ZoneGroupMember')}
    out = {}
    for m in root.iter('ZoneGroupMember'):
        if m.get('Invisible') != '1':
            continue
        uuid = m.get('UUID')
        chan = m.get('ChannelMapSet') or m.get('HTSatChanMapSet') or ''
        partner = next((u for u in re.findall(r'(RINCON_[0-9A-Fa-f]+)', chan) if u != uuid), None)
        out[uuid] = {'name': html.unescape(m.get('ZoneName') or ''), 'partner': partner,
                     'partner_present': bool(partner and partner in present)}
    return out


def crossfade(ip, on=None):
    """Read or set the speaker's own crossfade. -> bool"""
    if on is not None:
        soap(ip, 'AVTransport', 'SetCrossfadeMode',
             f'<InstanceID>0</InstanceID><CrossfadeMode>{1 if on else 0}</CrossfadeMode>')
    x = soap(ip, 'AVTransport', 'GetCrossfadeMode', '<InstanceID>0</InstanceID>')
    return (_val(x, 'CrossfadeMode') or '0') == '1'


def sleep_timer(ip, seconds=None):
    """Read or set the speaker's own sleep timer; 0 cancels. -> seconds left or 0.

    The speaker's timer, not the page's: it fires (with the speaker's own fade)
    even after the phone that set it has gone to sleep."""
    if seconds is not None:
        val = hms(int(seconds)) if seconds > 0 else ''
        soap(ip, 'AVTransport', 'ConfigureSleepTimer',
             f'<InstanceID>0</InstanceID><NewSleepTimerDuration>{val}</NewSleepTimerDuration>')
    x = soap(ip, 'AVTransport', 'GetRemainingSleepTimerDuration', '<InstanceID>0</InstanceID>')
    left = _val(x, 'RemainingSleepTimerDuration') or ''
    if not left:
        return 0
    parts = [int(p) for p in left.split(':')]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def join(ip, coordinator_uuid):
    return soap(ip, 'AVTransport', 'SetAVTransportURI',
                f'<InstanceID>0</InstanceID>'
                f'<CurrentURI>x-rincon:{coordinator_uuid}</CurrentURI>'
                f'<CurrentURIMetaData></CurrentURIMetaData>')

def unjoin(ip):
    return soap(ip, 'AVTransport', 'BecomeCoordinatorOfStandaloneGroup',
                '<InstanceID>0</InstanceID>')

if __name__ == '__main__':
    print('searching for Sonos players…')
    t0 = time.time()
    ds = discover()
    print(f'found {len(ds)} in {time.time()-t0:.1f}s')
    for d in ds:
        print(f"   {d['name']:22} {d['ip']:16} {d['model']}")
    if ds:
        print('\nzone groups:')
        for g in zone_groups(ds[0]['ip']):
            print(f"   {g['name']}  (coordinator {g['coordinator_ip']})")
