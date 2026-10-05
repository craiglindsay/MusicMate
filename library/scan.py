import os, sys, json, traceback
import mutagen

AUDIO_EXT = {'.mp3','.m4a','.m4p','.aac','.flac','.wma','.ogg','.wav','.aiff','.aif','.mp4','.alac','.ape','.wv','.m4b','.opus'}
IMG_EXT = {'.jpg','.jpeg','.png','.bmp','.gif','.tif','.tiff','.webp'}

def gettag(a, keys):
    for k in keys:
        try:
            v = a.get(k)
        except Exception:
            v = None
        if v:
            if isinstance(v, list):
                v = v[0]
            try:
                v = str(v)
            except Exception:
                v = None
            if v:
                return v
    return None

def scan(root, label, out):
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in ('$recycle.bin','system volume information')]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            try:
                size = os.path.getsize(full)
            except Exception:
                size = -1
            rec = {'src': label, 'rel': rel.replace('\\','/'), 'ext': ext, 'size': size}
            if ext in AUDIO_EXT:
                rec['kind'] = 'audio'
                try:
                    a = mutagen.File(full)
                    if a is None:
                        rec['err'] = 'unreadable'
                    else:
                        i = a.info
                        rec['len'] = round(getattr(i,'length',0) or 0, 1)
                        rec['br'] = int(getattr(i,'bitrate',0) or 0)
                        rec['sr'] = int(getattr(i,'sample_rate',0) or 0)
                        rec['ch'] = int(getattr(i,'channels',0) or 0)
                        rec['codec'] = type(i).__name__
                        brm = getattr(i,'bitrate_mode',None)
                        if brm is not None: rec['brmode'] = str(brm)
                        rec['artist']  = gettag(a, ['artist','TPE1','\xa9ART','Author'])
                        rec['albumartist'] = gettag(a, ['albumartist','TPE2','aART','WM/AlbumArtist'])
                        rec['album']   = gettag(a, ['album','TALB','\xa9alb','WM/AlbumTitle'])
                        rec['title']   = gettag(a, ['title','TIT2','\xa9nam','Title'])
                        rec['track']   = gettag(a, ['tracknumber','TRCK','trkn','WM/TrackNumber'])
                        rec['disc']    = gettag(a, ['discnumber','TPOS','disk','WM/PartOfSet'])
                        rec['date']    = gettag(a, ['date','TDRC','TYER','\xa9day','WM/Year','year','originaldate'])
                        rec['genre']   = gettag(a, ['genre','TCON','\xa9gen','WM/Genre'])
                        # embedded art
                        art = 0
                        try:
                            if hasattr(a,'tags') and a.tags is not None:
                                t = a.tags
                                if hasattr(t,'getall'):
                                    art = len(t.getall('APIC'))
                                elif 'covr' in t:
                                    art = len(t['covr'])
                                elif hasattr(a,'pictures'):
                                    art = len(a.pictures)
                                elif 'WM/Picture' in t:
                                    art = len(t['WM/Picture'])
                            if not art and hasattr(a,'pictures'):
                                art = len(a.pictures)
                        except Exception:
                            pass
                        rec['art'] = art
                        # protected iTunes formats that will not play
                        if ext in ('.m4p','.m4b'):
                            rec['protected'] = 1
                        try:
                            if hasattr(a,'tags') and a.tags is not None and '----:com.apple.iTunes:iTunesMediaType' in a.tags:
                                pass
                        except Exception:
                            pass
                except Exception as e:
                    rec['err'] = type(e).__name__ + ':' + str(e)[:120]
            elif ext in IMG_EXT:
                rec['kind'] = 'image'
            else:
                rec['kind'] = 'other'
            out.write(json.dumps(rec, ensure_ascii=False) + '\n')
            n += 1
            if n % 2000 == 0:
                print(f'{label}: {n}', file=sys.stderr, flush=True)
    print(f'{label}: DONE {n}', file=sys.stderr, flush=True)

if __name__ == '__main__':
    outpath = sys.argv[1]
    with open(outpath, 'w', encoding='utf-8') as out:
        music = os.environ.get('MUSICMATE_ROOT') or r'D:\Music'
        for label, root in [('VA', os.path.join(music, 'VariousArtists'))]:
            scan(root, label, out)
