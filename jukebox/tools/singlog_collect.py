"""Collect a phone's sing-along log while someone sings.

Open the phone on /?sing=1&log=1; it posts {sing: [...]} batches to /api/diag,
which keeps only the last five posts. This polls it and appends every new
record to a JSONL file, for as long as it runs (default ten minutes).

    python tools/singlog_collect.py [out.jsonl] [--minutes 10] [--base http://127.0.0.1]
"""
import json, time, argparse, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument('out', nargs='?', default='singlog.jsonl')
ap.add_argument('--minutes', type=float, default=10)
ap.add_argument('--base', default='http://127.0.0.1')
a = ap.parse_args()

seen = set()
with open(a.out, 'a', encoding='utf8') as out:
    end = time.time() + a.minutes * 60
    while time.time() < end:
        try:
            for e in json.load(urllib.request.urlopen(a.base + '/api/diag', timeout=3)):
                for r in e.get('sing') or []:
                    k = (r.get('ms'), r.get('ev'))
                    if k not in seen:
                        seen.add(k)
                        out.write(json.dumps(r) + '\n')
                        out.flush()
        except Exception:
            pass
        time.sleep(0.7)
