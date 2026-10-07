"""Post the newest recalls from the site's RSS feed to Bluesky, as link cards.

Needs BLUESKY_HANDLE and BLUESKY_APP_PASSWORD (repository secrets). Does nothing without them.
Recalls already posted (found in the account's recent posts) are skipped, so re-runs are safe.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

PDS = 'https://bsky.social/xrpc/'
MAX_POSTS = int(os.environ.get('BLUESKY_MAX_POSTS', '4'))
MAX_AGE_DAYS = 7
# Recalls people search for most come first.
PRIORITY = ['Vehicles', 'Food', 'Consumer products', 'Drugs', 'Medical devices']
EMOJI = {'Vehicles': '🚗', 'Food': '🍽️', 'Consumer products': '🧸', 'Drugs': '💊', 'Medical devices': '🩺'}


def call(method: str, body: dict | None = None, token: str | None = None, query: str = '') -> dict:
    req = urllib.request.Request(PDS + method + query, method='POST' if body is not None else 'GET',
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json'})
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def feed_items(site: str) -> list[dict]:
    with urllib.request.urlopen(site.rstrip('/') + '/feed.xml', timeout=30) as resp:
        root = ET.fromstring(resp.read())
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    items = []
    for it in root.iter('item'):
        date = parsedate_to_datetime(it.findtext('pubDate'))
        if date < cutoff:
            continue
        items.append({'title': it.findtext('title') or '', 'link': it.findtext('link') or '',
                      'category': it.findtext('category') or '', 'description': it.findtext('description') or '',
                      'date': date})
    rank = {c: i for i, c in enumerate(PRIORITY)}
    items.sort(key=lambda x: (rank.get(x['category'], 9), -x['date'].timestamp()))
    return items


def shorten(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rsplit(' ', 1)[0] + '…'


def main() -> None:
    handle, password = os.environ.get('BLUESKY_HANDLE'), os.environ.get('BLUESKY_APP_PASSWORD')
    if not handle or not password:
        print('BLUESKY_HANDLE / BLUESKY_APP_PASSWORD not set; skipping.')
        return
    site = os.environ['SITE_URL']
    session = call('com.atproto.server.createSession', {'identifier': handle, 'password': password})
    token, did = session['accessJwt'], session['did']

    posted: set[str] = set()
    recent = call('app.bsky.feed.getAuthorFeed', token=token, query=f'?actor={did}&limit=100')
    for p in recent.get('feed', []):
        uri = (((p.get('post') or {}).get('record') or {}).get('embed') or {}).get('external', {}).get('uri')
        if uri:
            posted.add(uri)

    count = 0
    for item in feed_items(site):
        if count >= MAX_POSTS:
            break
        if item['link'] in posted:
            continue
        text = f"{EMOJI.get(item['category'], '⚠️')} Recall: {shorten(item['title'], 240)}"
        record = {
            '$type': 'app.bsky.feed.post',
            'text': text,
            'createdAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            'langs': ['en'],
            'embed': {'$type': 'app.bsky.embed.external',
                      'external': {'uri': item['link'], 'title': shorten(item['title'], 200),
                                   'description': shorten(item['description'], 280)}},
        }
        try:
            call('com.atproto.repo.createRecord', {'repo': did, 'collection': 'app.bsky.feed.post', 'record': record},
                 token=token)
        except urllib.error.HTTPError as exc:
            print(f'Post failed: HTTP {exc.code} {exc.read()[:200]!r}', file=sys.stderr)
            continue
        count += 1
        print('Posted:', item['link'])
    print(f'{count} post(s) made.')


if __name__ == '__main__':
    main()
