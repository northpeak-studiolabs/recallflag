#!/usr/bin/env python3
"""Quick QA for dist/: tag balance, required SEO tags, JSON-LD parses, no broken internal links.

Usage: SITE_URL=... python check_site.py [dist]   (exit 1 on any error)
"""

from __future__ import annotations

import collections
import json
import os
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
OPTIONAL_END = {'li', 'dt', 'dd', 'p', 'tr', 'td', 'th', 'option', 'thead', 'tbody'}


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors, self.links = [], [], []
        self.title = self.desc = self.canonical = None
        self.h1 = 0
        self.ids = collections.Counter()
        self.in_ld = False
        self.ld = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get('id'):
            self.ids[a['id']] += 1
        if tag == 'a' and a.get('href'):
            self.links.append(a['href'])
        if tag in ('link', 'script', 'img') and (a.get('href') or a.get('src')) and a.get('rel') != 'canonical':
            self.links.append(a.get('href') or a.get('src'))
        if tag == 'meta' and a.get('name') == 'description':
            self.desc = a.get('content')
        if tag == 'link' and a.get('rel') == 'canonical':
            self.canonical = a.get('href')
        if tag == 'h1':
            self.h1 += 1
        if tag == 'title':
            self.in_title = True
            self.title = ''
        if tag == 'script' and a.get('type') == 'application/ld+json':
            self.in_ld = True
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        self.in_title = False if tag == 'title' else self.in_title
        self.in_ld = False if tag == 'script' else self.in_ld
        while self.stack and self.stack[-1] != tag and self.stack[-1] in OPTIONAL_END:
            self.stack.pop()
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f'unexpected </{tag}> (open: {self.stack[-3:]})')
            return
        self.stack.pop()

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.in_ld:
            self.ld.append(data)


def main() -> None:
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent / 'dist')
    base = urlparse(os.environ.get('SITE_URL', 'http://localhost:8000')).path.rstrip('/')
    errors = collections.defaultdict(list)
    titles, descs = collections.Counter(), collections.Counter()
    broken = collections.Counter()
    files = list(dist.rglob('*.html'))
    for f in files:
        rel = '/' + str(f.relative_to(dist))
        p = Page()
        p.feed(f.read_text(encoding='utf-8'))
        p.close()
        left = [t for t in p.stack if t not in OPTIONAL_END]
        if left:
            errors['unclosed'].append(f'{rel}: {left}')
        for e in p.errors:
            errors['tags'].append(f'{rel}: {e}')
        if not p.title:
            errors['no-title'].append(rel)
        if not p.desc:
            errors['no-description'].append(rel)
        if p.h1 != 1:
            errors['h1-count'].append(f'{rel}: {p.h1}')
        if rel not in ('/404.html',) and not p.canonical and 'search' not in rel:
            errors['no-canonical'].append(rel)
        for k, n in p.ids.items():
            if n > 1:
                errors['dup-id'].append(f'{rel}: #{k}')
        for block in ''.join(p.ld).split('\n'):
            if block.strip():
                try:
                    json.loads(block)
                except ValueError as exc:
                    errors['json-ld'].append(f'{rel}: {exc}')
        if not rel.endswith('/404.html') and '/search/' not in rel:
            titles[p.title] += 1
            descs[p.desc] += 1
        for href in p.links:
            u = urlparse(href)
            if u.scheme or u.netloc or href.startswith(('#', 'mailto:')):
                continue
            path = unquote(u.path)
            if base and path.startswith(base):
                path = path[len(base):] or '/'
            target = dist / path.lstrip('/')
            if path.endswith('/'):
                target = target / 'index.html'
            if not target.exists():
                broken[href] += 1
    for t, n in titles.items():
        if n > 1:
            errors['dup-title'].append(f'{n}x {t}')
    for d, n in descs.items():
        if n > 1:
            errors['dup-description'].append(f'{n}x {d}')
    for href, n in broken.items():
        errors['broken-link'].append(f'{href} ({n} refs)')
    print(f'Checked {len(files):,} HTML files.')
    for k, v in errors.items():
        print(f'  {k}: {len(v)}')
        for line in v[:5]:
            print(f'    {line}')
    for xml in ['sitemap.xml', 'feed.xml', *[f'sitemaps/{x.name}' for x in (dist / 'sitemaps').glob('*.xml')]]:
        import xml.etree.ElementTree as ET
        ET.parse(dist / xml)
    print('  sitemap/feed XML parse OK')
    sys.exit(1 if errors else 0)


if __name__ == '__main__':
    main()
