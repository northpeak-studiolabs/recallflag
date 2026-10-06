"""Tell Bing, Yandex, Seznam and other IndexNow search engines about new or changed pages.

Usage: SITE_URL=https://... python indexnow.py [--all]
Without --all, only sitemap URLs with a lastmod in the last 3 days are sent.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
KEY_FILE = next((HERE / 'static_root').glob('*.txt'))
KEY = KEY_FILE.read_text().strip()
URL_RE = re.compile(r'<url><loc>([^<]+)</loc>(?:<lastmod>([^<]+)</lastmod>)?</url>')


def main() -> None:
    site = os.environ['SITE_URL'].rstrip('/')
    send_all = '--all' in sys.argv or os.environ.get('INDEXNOW_ALL') == 'true'
    since = (date.today() - timedelta(days=3)).isoformat()
    urls = [site + '/']
    for f in sorted(glob.glob(str(HERE / 'dist' / 'sitemaps' / '*.xml'))):
        for loc, lastmod in URL_RE.findall(Path(f).read_text(encoding='utf-8')):
            if send_all or (lastmod and lastmod[:10] >= since):
                urls.append(loc)
    urls = list(dict.fromkeys(urls))
    host = urlparse(site).hostname
    print(f'Submitting {len(urls)} URL(s) to IndexNow ({"all" if send_all else "changed since " + since})')
    for i in range(0, len(urls), 10000):
        body = json.dumps({'host': host, 'key': KEY, 'keyLocation': f'{site}/{KEY_FILE.name}',
                           'urlList': urls[i:i + 10000]}).encode()
        req = urllib.request.Request('https://api.indexnow.org/indexnow', data=body, method='POST',
                                     headers={'Content-Type': 'application/json; charset=utf-8'})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                print(f'  batch {i // 10000 + 1}: HTTP {resp.status}')
        except urllib.error.HTTPError as exc:
            # Never fail the deploy over this; 202 = accepted, 403/422 = key not verified yet.
            print(f'  batch {i // 10000 + 1}: HTTP {exc.code} {exc.read()[:200]!r}')


if __name__ == '__main__':
    main()
