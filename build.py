#!/usr/bin/env python3
"""Static site generator for RecallFlag: data/recalls.json -> dist/.

Deps: Jinja2 only. Usage:
    SITE_URL=https://example.com python build.py [--ads placeholder|off] [--out dist]

SITE_URL may include a path (e.g. https://user.github.io/recall-watch) - every internal
link is prefixed with that path so the site works on a GitHub Pages project URL too.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import html
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch import STATES, firm_key  # noqa: E402

HERE = Path(__file__).resolve().parent
SITE_NAME = os.environ.get('SITE_NAME', 'RecallFlag')
CONTACT_URL = os.environ.get('CONTACT_URL', '')
SIGNUP = os.environ.get('SIGNUP', 'off') == 'on'
PER_PAGE = 60
SITEMAP_MAX = 45000  # protocol limit is 50,000 URLs / 50 MB per file

SOURCES = {
    'fda-food': {'label': 'FDA (food)', 'agency': 'FDA', 'code': 'F'},
    'fda-drug': {'label': 'FDA (drugs)', 'agency': 'FDA', 'code': 'D'},
    'fda-device': {'label': 'FDA (medical devices)', 'agency': 'FDA', 'code': 'M'},
    'cpsc': {'label': 'CPSC (consumer products)', 'agency': 'CPSC', 'code': 'C'},
    'nhtsa': {'label': 'NHTSA (vehicles)', 'agency': 'NHTSA', 'code': 'V'},
}
CATEGORY_INTRO = {
    'Food': 'Food and dietary-supplement recalls reported in FDA enforcement reports, including undeclared allergens, '
            'Listeria, Salmonella and foreign-material contamination.',
    'Drugs': 'Prescription and over-the-counter drug recalls from FDA enforcement reports: sterility problems, '
             'contamination, potency failures and labeling errors.',
    'Medical devices': 'Medical device recalls from FDA enforcement reports, from surgical kits to imaging systems '
                       'and software issues.',
    'Consumer products': 'Consumer product recalls announced by the U.S. Consumer Product Safety Commission (CPSC): '
                         'toys, furniture, baby products, electronics, appliances and more.',
    'Vehicles': 'Vehicle, equipment, tire and child-seat safety recalls filed with the National Highway Traffic '
                'Safety Administration (NHTSA).',
}
CLASS_INFO = {
    'Class I': 'Class I is the most serious FDA recall class: there is a reasonable probability that using or being '
               'exposed to the product will cause serious adverse health consequences or death.',
    'Class II': 'Class II means use of or exposure to the product may cause temporary or medically reversible '
                'adverse health consequences, or the probability of serious harm is remote.',
    'Class III': 'Class III means use of or exposure to the product is not likely to cause adverse health '
                 'consequences (for example, a labeling violation).',
}
DATA_SOURCES = [
    ('openFDA food, drug & device enforcement reports', 'https://open.fda.gov/apis/'),
    ('CPSC Recalls API (SaferProducts.gov)', 'https://www.saferproducts.gov/'),
    ('NHTSA recalls (data.transportation.gov)', 'https://data.transportation.gov/Automobiles/Recalls-Data/6axg-epim'),
    ('NHTSA recall flat files', 'https://www.nhtsa.gov/nhtsa-datasets-and-apis'),
]


# ---------------------------------------------------------------- text helpers

def slugify(text: str | None, maxlen: int = 70) -> str:
    text = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode().lower()
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    if len(text) > maxlen:
        text = text[:maxlen].rsplit('-', 1)[0]
    return text or 'x'


def shorten(text: str | None, n: int) -> str:
    text = re.sub(r'\s+', ' ', text or '').strip().replace('……', '…')
    if len(text) <= n:
        return text
    cut = text[:n]
    for sep in ('. ', ', ', '; ', ' '):
        i = cut.rfind(sep)
        if i > n * 0.55:
            return cut[:i].rstrip(' ,;.…') + '…'
    return cut.rstrip(' …') + '…'


def product_name(r: dict) -> str:
    """A short human product name for headings."""
    if r['source'] == 'nhtsa':
        return vehicle_label(r) or r.get('title') or 'Vehicle'
    if r['source'] == 'cpsc':
        p = r.get('product') or r.get('title') or 'Product'
        return shorten(p, 80)
    full = r.get('product') or ''
    full = re.sub(r'^\W*((product|device|brand)\s*(description|name)?|description|item)\s*[:/\-]\s*', '', full, flags=re.I)
    p = re.split(r'(?<=[a-z\)])\.\s|\s\|\s|;\s|\bNDC\b|\bUPC\b|\bLot\b|\bREF\b|\bCatalog\b', full, maxsplit=1)[0]
    p = p.strip(' ,.;:-/"')
    if len(p) < 8:
        p = full.strip(' ,.;:-/"')
    return shorten(p, 75) or f"{r['category']} product {r['id']}"


def vtitle(text: str) -> str:
    """Title-case a make/model but keep codes like EQE, CR-V, F-150, RAV4 and BMW upper-case."""
    def fix(w: str) -> str:
        if any(c.isdigit() for c in w) or len(w.strip('()+-/')) <= 3:
            return w
        return '-'.join(p.capitalize() for p in w.split('-'))
    return ' '.join(fix(w) for w in text.split())


def year_span(years: list[int]) -> str:
    if not years:
        return ''
    return str(years[0]) if years[0] == years[-1] else f'{years[0]}–{years[-1]}'


def vehicle_label(r: dict) -> str:
    vs = r.get('vehicles') or []
    if not vs:
        return ''
    first = vs[0]
    years = sorted({y for v in vs if v['make'] == first['make'] and v['model'] == first['model'] for y in v['years']})
    lab = f"{year_span(years)} {vtitle(first['make'])} {vtitle(first['model'])}".strip()
    others = len({(v['make'], v['model']) for v in vs}) - 1
    return lab + (f' and {others} more' if others > 0 else '')


def human_date(iso: str | None) -> str:
    if not iso:
        return ''
    d = dt.date.fromisoformat(iso[:10])
    return f'{d:%B} {d.day}, {d.year}'


# ---------------------------------------------------------------- site model

class Site:
    def __init__(self, data: dict, site_url: str, out: Path, ads: str):
        self.data = data
        self.site_url = site_url.rstrip('/')
        self.base = urlparse(self.site_url).path.rstrip('/')
        self.out = out
        self.ads = ads
        self.generated = data['generated']
        self.recalls = data['recalls']
        self.urls: list[tuple[str, str | None]] = []  # (path, lastmod)
        self.page_counts: collections.Counter = collections.Counter()
        self.titles: collections.Counter = collections.Counter()
        self.env = Environment(loader=FileSystemLoader(HERE / 'templates'),
                               autoescape=select_autoescape(['html', 'xml']), trim_blocks=True, lstrip_blocks=True)
        self.env.filters.update(human_date=human_date, shorten=shorten, url=self.url, slug=slugify)
        self.env.globals.update(site_name=SITE_NAME, contact_url=CONTACT_URL, signup=SIGNUP, url=self.url, abs_url=self.abs_url, ads=ads,
                                updated=self.generated, updated_human=self.updated_human(),
                                data_sources=DATA_SOURCES, sources=SOURCES, year=dt.date.today().year)

    def updated_human(self) -> str:
        t = dt.datetime.fromisoformat(self.generated)
        return f'{t:%B} {t.day}, {t.year} {t:%H:%M} UTC'

    def url(self, path: str) -> str:
        return self.base + path

    def abs_url(self, path: str) -> str:
        return self.site_url + path

    # -------------------------------------------------- enrichment / indexes
    def prepare(self) -> None:
        firm_names: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        for r in self.recalls:
            r['src'] = SOURCES[r['source']]
            r['name'] = product_name(r)
            r['brandKey'] = firm_key(r.get('brand')) or 'unknown'
            firm_names[r['brandKey']][r.get('brand') or 'Unknown firm'] += 1
            r['slug'] = f"{slugify(r['id'], 30)}-{slugify(r['name'], 60)}"
            r['path'] = f"/recall/{r['slug']}/"
            r['catSlug'] = slugify(r['category'])
            r['subSlug'] = slugify(r['subcategory']) if r.get('subcategory') else None
            if r['source'] == 'nhtsa':
                head = f"{r['name']} recall: {r.get('title') or r.get('product') or ''}".strip(': ')
            elif r['source'] == 'cpsc':
                head = r.get('title') or f"{r['name']} recall"
            else:
                head = f"{r['name']} recall"
            r['headline'] = shorten(head, 140)
        # Display name for each firm = its most common spelling.
        self.firm_display = {k: c.most_common(1)[0][0] for k, c in firm_names.items()}
        self.by_id = {r['id']: r for r in self.recalls}

        idx = collections.defaultdict(list)
        for r in self.recalls:  # already newest-first
            idx[('brand', r['brandKey'])].append(r)
            idx[('cat', r['catSlug'])].append(r)
            if r['subSlug']:
                idx[('sub', r['catSlug'], r['subSlug'])].append(r)
            idx[('month', r['date'][:7])].append(r)
            for s in r.get('states') or []:
                idx[('state', s)].append(r)
            if r.get('nationwide') and r['source'] != 'nhtsa':
                idx[('state', 'nationwide')].append(r)
            for t in r.get('hazardTags') or []:
                idx[('hazard', slugify(t))].append(r)
            if r.get('eventId'):
                idx[('event', r['eventId'])].append(r)
            seen = set()
            for v in r.get('vehicles') or []:
                if v['type'] != 'V':
                    continue
                mk, md = slugify(v['make'], 40), slugify(v['model'], 50)
                if (mk, md) in seen:
                    continue
                if (mk,) not in seen:
                    idx[('make', mk)].append(r)
                    seen.add((mk,))
                idx[('model', mk, md)].append(r)
                seen.add((mk, md))
        self.idx = idx
        self.make_names = {}
        self.model_names = {}
        for r in self.recalls:
            for v in r.get('vehicles') or []:
                mk, md = slugify(v['make'], 40), slugify(v['model'], 50)
                self.make_names.setdefault(mk, vtitle(v['make']))
                self.model_names.setdefault((mk, md), vtitle(v['model']))
        # Brands get a hub page only with >= 2 recalls (single-recall hubs would be thin duplicates).
        self.brand_hubs = {k for (kind, *rest), v in idx.items() if kind == 'brand' for k in rest
                           if len(v) >= 2 and k != 'unknown'}
        self.hazard_names = {slugify(t): t for r in self.recalls for t in r.get('hazardTags') or []}
        self.sub_names = {(r['catSlug'], r['subSlug']): r['subcategory'] for r in self.recalls if r['subSlug']}
        for r in self.recalls:
            r['brandName'] = self.firm_display[r['brandKey']]
            r['brandPath'] = f"/brand/{slugify(r['brandKey'])}/" if r['brandKey'] in self.brand_hubs else None

    # -------------------------------------------------- rendering helpers
    def write(self, path: str, html_text: str, lastmod: str | None = None, kind: str = 'other',
              sitemap: bool = True) -> None:
        target = self.out / path.lstrip('/')
        if path.endswith('/'):
            target = target / 'index.html'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html_text, encoding='utf-8')
        self.page_counts[kind] += 1
        if sitemap:
            self.urls.append((path, lastmod))

    def render(self, tpl: str, **ctx) -> str:
        title = ctx.get('title', '')
        self.titles[title] += 1
        return self.env.get_template(tpl).render(**ctx)

    def jsonld(self, *objs) -> str:
        return '\n'.join(json.dumps(o, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/') for o in objs)

    def breadcrumb_ld(self, crumbs: list[tuple[str, str | None]]) -> dict:
        return {'@context': 'https://schema.org', '@type': 'BreadcrumbList', 'itemListElement': [
            {'@type': 'ListItem', 'position': i + 1, 'name': n, **({'item': self.abs_url(p)} if p else {})}
            for i, (n, p) in enumerate(crumbs)]}

    def summary(self, items: list[dict]) -> str:
        if not items:
            return ''
        tags = collections.Counter(t for r in items for t in r.get('hazardTags') or [])
        srcs = collections.Counter(r['src']['agency'] for r in items)
        classes = collections.Counter(r['classification'] for r in items if r.get('classification'))
        parts = [f"{len(items):,} recall{'s' if len(items) != 1 else ''} published between "
                 f"{human_date(items[-1]['date'])} and {human_date(items[0]['date'])}"]
        if len(srcs) > 1:
            parts[0] += ' (' + ', '.join(f'{n:,} {a}' for a, n in srcs.most_common()) + ')'
        parts[0] += '.'
        if tags:
            parts.append('Most common issues: ' + ', '.join(f'{t.lower()} ({n})' for t, n in tags.most_common(4)) + '.')
        if classes.get('Class I'):
            parts.append(f"{classes['Class I']:,} {'were' if classes['Class I'] > 1 else 'was'} FDA Class I, the most serious level.")
        return ' '.join(parts)

    def hub(self, path: str, title: str, h1: str, intro: str, items: list[dict], crumbs, kind: str,
            extra_links: list[tuple[str, str]] | None = None, link_groups=None) -> None:
        pages = max(1, -(-len(items) // PER_PAGE))
        for p in range(1, pages + 1):
            chunk = items[(p - 1) * PER_PAGE:p * PER_PAGE]
            ppath = path if p == 1 else f'{path}page/{p}/'
            t = title if p == 1 else f'{title} – page {p}'
            desc = shorten(f"{intro} {self.summary(items)}" if p == 1 else
                           f"{h1}, page {p} of {pages}: recalls {p * PER_PAGE - PER_PAGE + 1}–{(p - 1) * PER_PAGE + len(chunk)}.", 158)
            ld = {'@context': 'https://schema.org', '@type': 'CollectionPage', 'name': t, 'description': desc,
                  'url': self.abs_url(ppath), 'dateModified': items[0]['date'] if items else None,
                  'mainEntity': {'@type': 'ItemList', 'numberOfItems': len(chunk), 'itemListElement': [
                      {'@type': 'ListItem', 'position': i + 1, 'url': self.abs_url(r['path']), 'name': r['headline']}
                      for i, r in enumerate(chunk)]}}
            html_text = self.render('hub.html', title=t, description=desc, canonical=ppath, h1=h1,
                                    intro=intro if p == 1 else '', summary=self.summary(items) if p == 1 else '',
                                    items=chunk, crumbs=crumbs, page=p, pages=pages, base_path=path,
                                    extra_links=extra_links if p == 1 else None,
                                    link_groups=link_groups if p == 1 else None,
                                    jsonld=self.jsonld(ld, self.breadcrumb_ld(crumbs)))
            self.write(ppath, html_text, items[0]['date'] if items else None, kind if p == 1 else kind + '-page')

    # -------------------------------------------------- pages
    def related(self, r: dict) -> list[tuple[str, str | None, list[dict]]]:
        used = {r['id']}
        groups = []

        def take(key, n, title, more=None):
            out = []
            for x in self.idx.get(key, []):
                if x['id'] not in used:
                    out.append(x)
                    used.add(x['id'])
                if len(out) >= n:
                    break
            if out:
                groups.append((title, more, out))

        if r.get('eventId') and len(self.idx[('event', r['eventId'])]) > 1:
            take(('event', r['eventId']), 10, 'Other products in the same recall event')
        take(('brand', r['brandKey']), 5, f"More recalls from {r['brandName']}", r['brandPath'])
        for v in (r.get('vehicles') or [])[:1]:
            mk, md = slugify(v['make'], 40), slugify(v['model'], 50)
            take(('model', mk, md), 5, f"Other {vtitle(v['make'])} {vtitle(v['model'])} recalls",
                 f'/vehicle/{mk}/{md}/')
        if r['subSlug']:
            take(('sub', r['catSlug'], r['subSlug']), 5, f"Recent {r['subcategory']} recalls",
                 f"/category/{r['catSlug']}/{r['subSlug']}/")
        if r.get('hazardTags'):
            t = r['hazardTags'][0]
            take(('hazard', slugify(t)), 5, f'Other recalls involving {t.lower()}', f'/hazard/{slugify(t)}/')
        take(('cat', r['catSlug']), 5, f"Latest {r['category'].lower()} recalls", f"/category/{r['catSlug']}/")
        return groups

    def recall_pages(self) -> None:
        for r in self.recalls:
            name = r['name']
            year = r['date'][:4]
            if r['source'] == 'nhtsa':
                title = f"{shorten(name, 45)} Recall: {shorten(r.get('title') or '', 40)}"
            elif r['source'] == 'cpsc':
                title = shorten(r.get('title') or name, 75)
            else:
                title = f"{shorten(name, 55)} Recall ({r.get('classification') or year})"
            r['_title'] = title
            reason = r.get('reason') or r.get('hazard') or ''
            if r['source'] == 'cpsc' and r.get('hazard'):
                reason = r['hazard']
            r['_desc'] = shorten(f"{human_date(r['date'])}: {r['brandName']} recalled {shorten(name, 60)}. {reason}", 158)
        # Make titles / descriptions unique by appending the recall number on collisions.
        for field in ('_title', '_desc'):
            c = collections.Counter(r[field] for r in self.recalls)
            for r in self.recalls:
                if c[r[field]] > 1:
                    r[field] = (shorten(r[field], 140 if field == '_desc' else 60) + f" ({r['id']})")

        for r in self.recalls:
            crumbs = [('Home', '/'), (r['category'], f"/category/{r['catSlug']}/")]
            if r['subSlug']:
                crumbs.append((r['subcategory'], f"/category/{r['catSlug']}/{r['subSlug']}/"))
            crumbs.append((shorten(r['name'], 50), None))
            article = {
                '@context': 'https://schema.org', '@type': 'Article', 'headline': shorten(r['headline'], 110),
                'description': r['_desc'], 'datePublished': r['date'], 'dateModified': self.generated[:10],
                'mainEntityOfPage': self.abs_url(r['path']),
                'author': {'@type': 'Organization', 'name': SITE_NAME, 'url': self.abs_url('/')},
                'publisher': {'@type': 'Organization', 'name': SITE_NAME, 'url': self.abs_url('/')},
                'isBasedOn': r.get('link'),
                'about': {'@type': 'Product', 'name': shorten(r['name'], 150),
                          **({'brand': {'@type': 'Brand', 'name': r['brandName']}} if r.get('brand') else {})},
            }
            if r.get('image'):
                article['image'] = r['image']
            states = [(s, STATES[s]) for s in r.get('states') or []]
            html_text = self.render(
                'recall.html', title=r['_title'], description=r['_desc'], canonical=r['path'], r=r,
                crumbs=crumbs, related=self.related(r), states=states,
                class_info=CLASS_INFO.get(r.get('classification') or ''),
                vehicles=self.vehicle_rows(r), jsonld=self.jsonld(article, self.breadcrumb_ld(crumbs)))
            self.write(r['path'], html_text, r['date'], 'recall')

    def vehicle_rows(self, r: dict) -> list[dict]:
        rows = []
        for v in r.get('vehicles') or []:
            mk, md = slugify(v['make'], 40), slugify(v['model'], 50)
            rows.append({'make': vtitle(v['make']), 'model': vtitle(v['model']), 'years': year_span(v['years']),
                         'path': f'/vehicle/{mk}/{md}/' if v['type'] == 'V' and ('model', mk, md) in self.idx else None})
        return rows

    def category_pages(self) -> None:
        cats = collections.OrderedDict()
        for r in self.recalls:
            cats.setdefault(r['catSlug'], r['category'])
        order = ['food', 'drugs', 'medical-devices', 'consumer-products', 'vehicles']
        self.cats = [(c, cats[c], len(self.idx[('cat', c)])) for c in order if c in cats]
        for cslug, cname, _ in self.cats:
            subs = sorted({(k[2], self.sub_names[(cslug, k[2])], len(v)) for k, v in self.idx.items()
                           if k[0] == 'sub' and k[1] == cslug}, key=lambda x: -x[2])
            links = [(f'{n} ({c:,})', f'/category/{cslug}/{s}/') for s, n, c in subs]
            self.hub(f'/category/{cslug}/', f'{cname} Recalls – Latest U.S. {cname} Recall List',
                     f'{cname} recalls', CATEGORY_INTRO.get(cname, ''), self.idx[('cat', cslug)],
                     [('Home', '/'), (cname, None)], 'category',
                     link_groups=[('Browse by type', links)] if len(links) > 1 else None)
            for s, n, _ in subs:
                items = self.idx[('sub', cslug, s)]
                if n in CLASS_INFO:
                    t, h, intro = (f'FDA {n} {cname} Recalls', f'{n} {cname.lower()} recalls',
                                   f'FDA {n} recalls of {cname.lower()}, newest first. {CLASS_INFO[n]}')
                else:
                    t, h, intro = (f'{n} Recalls ({cname})', f'{n} recalls',
                                   f'Recalls of {n.lower()} in the {cname.lower()} category, newest first.')
                self.hub(f'/category/{cslug}/{s}/', t, h, intro,
                         items, [('Home', '/'), (cname, f'/category/{cslug}/'), (n, None)], 'subcategory')

    def brand_pages(self) -> None:
        rows = []
        for key in self.brand_hubs:
            items = self.idx[('brand', key)]
            name = self.firm_display[key]
            path = f'/brand/{slugify(key)}/'
            rows.append((name, path, len(items)))
            cats = collections.Counter(r['category'] for r in items)
            intro = (f"All {name} recalls we track from FDA, CPSC and NHTSA data. "
                     f"Categories: {', '.join(f'{c.lower()} ({n})' for c, n in cats.most_common())}.")
            self.hub(path, f'{shorten(name, 45)} Recalls – Full List', f'{name} recalls', intro, items,
                     [('Home', '/'), ('Brands & firms', '/brand/'), (shorten(name, 50), None)], 'brand')
        rows.sort(key=lambda x: x[0].lower())
        groups = collections.OrderedDict()
        for name, path, n in rows:
            letter = name[0].upper() if name[0].isalpha() else '#'
            groups.setdefault(letter, []).append((f'{name} ({n})', path))
        html_text = self.render('index_list.html', title='Recalls by Brand and Company – A to Z',
                                description=f'Browse product recalls for {len(rows):,} brands and companies with more than one recall, A to Z.',
                                canonical='/brand/', h1='Recalls by brand & company',
                                intro='Companies and brands with two or more recalls in the last two years. '
                                      'Use the search box to find a firm with a single recall.',
                                groups=list(groups.items()), crumbs=[('Home', '/'), ('Brands & firms', None)],
                                jsonld=self.jsonld(self.breadcrumb_ld([('Home', '/'), ('Brands & firms', None)])))
        self.write('/brand/', html_text, None, 'index')
        self.top_brands = sorted(rows, key=lambda x: -x[2])[:24]

    def hazard_pages(self) -> None:
        rows = []
        for hs, name in sorted(self.hazard_names.items()):
            items = self.idx[('hazard', hs)]
            rows.append((f'{name} ({len(items):,})', f'/hazard/{hs}/'))
            self.hub(f'/hazard/{hs}/', f'{name} Recalls – Latest U.S. Recalls for {name}', f'{name} recalls',
                     f'Recalls where the official reason or hazard mentions {name.lower()}. '
                     'Tags are assigned automatically from the agency text, so always read the official notice.',
                     items, [('Home', '/'), ('Hazards', '/hazard/'), (name, None)], 'hazard')
        self.hazard_rows = rows
        crumbs = [('Home', '/'), ('Hazards', None)]
        self.write('/hazard/', self.render('index_list.html', title='Recalls by Hazard – Listeria, Salmonella, Fire & More',
                                           description='Browse U.S. food, drug, product and vehicle recalls by hazard: Listeria, Salmonella, undeclared allergens, fire, choking, crash risk and more.',
                                           canonical='/hazard/', h1='Recalls by hazard', intro='', groups=[('', rows)],
                                           crumbs=crumbs, jsonld=self.jsonld(self.breadcrumb_ld(crumbs))), None, 'index')

    def month_pages(self) -> None:
        months = sorted({k[1] for k in self.idx if k[0] == 'month'}, reverse=True)
        rows = []
        for m in months:
            d = dt.date.fromisoformat(m + '-01')
            label = f'{d:%B %Y}'
            items = self.idx[('month', m)]
            rows.append((f'{label} ({len(items):,})', f'/month/{m}/'))
            self.hub(f'/month/{m}/', f'Product Recalls in {label} – All U.S. Recalls', f'Recalls in {label}',
                     f'Every FDA, CPSC and NHTSA recall published in {label}.', items,
                     [('Home', '/'), ('By month', '/month/'), (label, None)], 'month')
        self.month_rows = rows
        crumbs = [('Home', '/'), ('By month', None)]
        self.write('/month/', self.render('index_list.html', title='Recall Archive by Month',
                                          description='Monthly archive of U.S. product, food, drug, medical device and vehicle recalls.',
                                          canonical='/month/', h1='Recall archive by month', intro='',
                                          groups=[('', rows)], crumbs=crumbs,
                                          jsonld=self.jsonld(self.breadcrumb_ld(crumbs))), None, 'index')

    def state_pages(self) -> None:
        rows = []
        nationwide = len(self.idx.get(('state', 'nationwide'), []))
        for code, name in sorted(STATES.items(), key=lambda x: x[1]):
            items = self.idx.get(('state', code), [])
            if not items:
                continue
            rows.append((f'{name} ({len(items):,})', f'/state/{code.lower()}/'))
            self.hub(f'/state/{code.lower()}/', f'{name} Recalls – Food, Drug & Product Recalls in {code}',
                     f'Recalls distributed in {name}',
                     f'Recalls whose official distribution list names {name}. A further {nationwide:,} recalls were '
                     f'distributed nationwide and may also affect {name}; see the nationwide list.',
                     items, [('Home', '/'), ('By state', '/state/'), (name, None)], 'state',
                     extra_links=[('Nationwide recalls', '/state/nationwide/')])
        if nationwide:
            rows.insert(0, (f'Nationwide ({nationwide:,})', '/state/nationwide/'))
            self.hub('/state/nationwide/', 'Nationwide Recalls – Distributed Across the U.S.', 'Nationwide recalls',
                     'FDA and CPSC recalls whose distribution is described as nationwide.',
                     self.idx[('state', 'nationwide')], [('Home', '/'), ('By state', '/state/'), ('Nationwide', None)], 'state')
        crumbs = [('Home', '/'), ('By state', None)]
        self.write('/state/', self.render('index_list.html', title='Recalls by State – Where Recalled Products Were Sold',
                                          description='Find food, drug and consumer product recalls distributed in your state, based on official FDA distribution patterns.',
                                          canonical='/state/', h1='Recalls by state',
                                          intro='Based on the distribution pattern in FDA enforcement reports (and retailer notes in CPSC recalls). '
                                                'Vehicle recalls apply nationwide and are not listed by state.',
                                          groups=[('', rows)], crumbs=crumbs,
                                          jsonld=self.jsonld(self.breadcrumb_ld(crumbs))), None, 'index')

    def vehicle_pages(self) -> None:
        makes = sorted({k[1] for k in self.idx if k[0] == 'make'}, key=lambda m: self.make_names[m])
        make_rows = []
        for mk in makes:
            mname = self.make_names[mk]
            items = self.idx[('make', mk)]
            make_rows.append((f'{mname} ({len(items):,})', f'/vehicle/{mk}/'))
            models = sorted(((k[2], len(v)) for k, v in self.idx.items() if k[0] == 'model' and k[1] == mk),
                            key=lambda x: self.model_names[(mk, x[0])])
            links = [(f'{self.model_names[(mk, md)]} ({n})', f'/vehicle/{mk}/{md}/') for md, n in models]
            self.hub(f'/vehicle/{mk}/', f'{mname} Recalls – All {mname} Safety Recalls by Model',
                     f'{mname} recalls', f'NHTSA safety recalls covering {mname} vehicles, newest first. '
                     'Always confirm with your VIN on nhtsa.gov.', items,
                     [('Home', '/'), ('Vehicles', '/vehicle/'), (mname, None)], 'make',
                     link_groups=[('Models', links)] if links else None)
            for md, _ in models:
                mdname = self.model_names[(mk, md)]
                its = self.idx[('model', mk, md)]
                yrs = sorted({y for r in its for v in r['vehicles'] if slugify(v['make'], 40) == mk
                              and slugify(v['model'], 50) == md for y in v['years']})
                ys = f' ({year_span(yrs)})' if yrs else ''
                self.hub(f'/vehicle/{mk}/{md}/', f'{mname} {mdname} Recalls{ys}', f'{mname} {mdname} recalls',
                         f'NHTSA safety recalls for the {mname} {mdname}{ys}. Enter your VIN at nhtsa.gov/recalls '
                         'to see whether your vehicle has an open recall.', its,
                         [('Home', '/'), ('Vehicles', '/vehicle/'), (mname, f'/vehicle/{mk}/'), (mdname, None)], 'model',
                         extra_links=[('Check your VIN at NHTSA', 'https://www.nhtsa.gov/recalls')])
        crumbs = [('Home', '/'), ('Vehicles', None)]
        self.make_rows = make_rows
        self.write('/vehicle/', self.render('index_list.html', title='Vehicle Recalls by Make and Model',
                                            description=f'Browse NHTSA vehicle safety recalls for {len(makes)} makes, by model and model year.',
                                            canonical='/vehicle/', h1='Vehicle recalls by make',
                                            intro='Vehicle recalls from NHTSA. Pick a make to see models and model years.',
                                            groups=[('', make_rows)], crumbs=crumbs,
                                            jsonld=self.jsonld(self.breadcrumb_ld(crumbs))), None, 'index')

    def home(self) -> None:
        latest = self.recalls[:40]
        ld = {'@context': 'https://schema.org', '@type': 'WebSite', 'name': SITE_NAME, 'url': self.abs_url('/'),
              'potentialAction': {'@type': 'SearchAction',
                                  'target': self.abs_url('/search/') + '?q={search_term_string}',
                                  'query-input': 'required name=search_term_string'}}
        html_text = self.render('home.html', title=f'{SITE_NAME} – Search U.S. Food, Product, Drug & Vehicle Recalls',
                                description=f"Search {len(self.recalls):,} recent U.S. recalls from the FDA, CPSC and NHTSA. "
                                            'Updated daily: food, drugs, medical devices, consumer products and vehicles.',
                                canonical='/', latest=latest, cats=self.cats, top_brands=self.top_brands,
                                hazards=self.hazard_rows, months=self.month_rows[:6], counts=self.data['counts'],
                                total=len(self.recalls), class1=[r for r in self.recalls if r.get('classification') == 'Class I'][:8],
                                jsonld=self.jsonld(ld))
        self.write('/', html_text, self.recalls[0]['date'], 'home')
        crumbs = [('Home', '/'), ('Search', None)]
        self.write('/search/', self.render('search.html', title=f'Search Recalls – {SITE_NAME}',
                                           description='Search U.S. recalls by product, brand, company, vehicle make/model or recall number.',
                                           canonical='/search/', crumbs=crumbs, noindex=True,
                                           jsonld=self.jsonld(self.breadcrumb_ld(crumbs))), None, 'search', sitemap=False)
        crumbs = [('Home', '/'), ('About & data sources', None)]
        self.write('/about/', self.render('about.html', title=f'About {SITE_NAME} & Data Sources',
                                          description=f'How {SITE_NAME} collects recall data from the FDA, CPSC and NHTSA, how often it updates, and its limitations.',
                                          canonical='/about/', crumbs=crumbs, counts=self.data['counts'], since=self.data['since'],
                                          jsonld=self.jsonld(self.breadcrumb_ld(crumbs))), None, 'about')
        crumbs = [('Home', '/'), ('Privacy', None)]
        self.write('/privacy/', self.render('privacy.html', title=f'Privacy Policy – {SITE_NAME}',
                                            description=f'How {SITE_NAME} handles visitor data, cookies and advertising.',
                                            canonical='/privacy/', crumbs=crumbs,
                                            jsonld=self.jsonld(self.breadcrumb_ld(crumbs))), None, 'privacy')
        crumbs = [('Home', '/'), ('Contact', None)]
        self.write('/contact/', self.render('contact.html', title=f'Contact {SITE_NAME}',
                                            description=f'How to report an error or contact {SITE_NAME}.',
                                            canonical='/contact/', crumbs=crumbs,
                                            jsonld=self.jsonld(self.breadcrumb_ld(crumbs))), None, 'contact')
        self.write('/404.html', self.render('404.html', title='Page not found', description='Page not found.',
                                            canonical=None, noindex=True), None, '404', sitemap=False)

    def assets(self) -> None:
        dst = self.out / 'assets'
        dst.mkdir(parents=True, exist_ok=True)
        for f in (HERE / 'static').iterdir():
            shutil.copy(f, dst / f.name)
        # Compact client-side search index: [path-slug, date, label, brand, source code].
        index = []
        for r in self.recalls:
            label = r['headline']
            if r.get('vehicles'):
                label += ' ' + ' '.join(sorted({vtitle(f"{v['make']} {v['model']}") for v in r['vehicles']}))[:200]
            index.append([r['slug'], r['date'], shorten(label, 260), r['brandName'][:60], r['src']['code'], r['id']])
        (dst / 'search-index.json').write_text(json.dumps(index, ensure_ascii=False, separators=(',', ':')))

    def feeds(self) -> None:
        items = []
        for r in self.recalls[:60]:
            pub = dt.datetime.fromisoformat(r['date']).replace(tzinfo=dt.timezone.utc)
            items.append(f"""<item><title>{html.escape(r['headline'])}</title><link>{self.abs_url(r['path'])}</link>
<guid isPermaLink="true">{self.abs_url(r['path'])}</guid><pubDate>{format_datetime(pub)}</pubDate>
<category>{html.escape(r['category'])}</category><description>{html.escape(r['_desc'])}</description></item>""")
        now = format_datetime(dt.datetime.fromisoformat(self.generated))
        rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>
<title>{html.escape(SITE_NAME)} – latest U.S. recalls</title><link>{self.abs_url('/')}</link>
<atom:link href="{self.abs_url('/feed.xml')}" rel="self" type="application/rss+xml"/>
<description>Latest recalls from the FDA, CPSC and NHTSA.</description><language>en-us</language>
<lastBuildDate>{now}</lastBuildDate>
{''.join(items)}
</channel></rss>"""
        (self.out / 'feed.xml').write_text(rss, encoding='utf-8')

        smdir = self.out / 'sitemaps'
        smdir.mkdir(exist_ok=True)
        files = []
        for i in range(0, len(self.urls), SITEMAP_MAX):
            chunk = self.urls[i:i + SITEMAP_MAX]
            body = ''.join(f"<url><loc>{html.escape(self.abs_url(p))}</loc>{f'<lastmod>{m}</lastmod>' if m else ''}</url>\n"
                           for p, m in chunk)
            name = f'sitemap-{i // SITEMAP_MAX + 1}.xml'
            (smdir / name).write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                                      + body + '</urlset>\n', encoding='utf-8')
            files.append(name)
        idx = ''.join(f"<sitemap><loc>{self.abs_url('/sitemaps/' + f)}</loc><lastmod>{self.generated[:10]}</lastmod></sitemap>\n"
                      for f in files)
        (self.out / 'sitemap.xml').write_text('<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                                              + idx + '</sitemapindex>\n', encoding='utf-8')
        (self.out / 'robots.txt').write_text(f"User-agent: *\nAllow: /\nDisallow: {self.url('/search/')}\n\n"
                                             f"Sitemap: {self.abs_url('/sitemap.xml')}\n", encoding='utf-8')
        if os.environ.get('CNAME'):
            (self.out / 'CNAME').write_text(os.environ['CNAME'].strip() + '\n')
        (self.out / '.nojekyll').write_text('')
        self.sitemap_files = files

    def build(self) -> dict:
        if self.out.exists():
            shutil.rmtree(self.out)
        self.out.mkdir(parents=True)
        self.prepare()
        self.recall_pages()
        self.category_pages()
        self.brand_pages()
        self.hazard_pages()
        self.month_pages()
        self.state_pages()
        self.vehicle_pages()
        self.home()
        self.assets()
        self.feeds()
        dup_titles = sum(n - 1 for t, n in self.titles.items() if n > 1)
        return {'pages': dict(self.page_counts), 'total_pages': sum(self.page_counts.values()),
                'sitemap_urls': len(self.urls), 'sitemaps': self.sitemap_files, 'duplicate_titles': dup_titles}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=str(HERE / 'data' / 'recalls.json'))
    ap.add_argument('--out', default=str(HERE / 'dist'))
    ap.add_argument('--ads', choices=['placeholder', 'off'], default=os.environ.get('ADS', 'placeholder'))
    a = ap.parse_args()
    t0 = time.time()
    data = json.loads(Path(a.data).read_text())
    site = Site(data, os.environ.get('SITE_URL', 'http://localhost:8000'), Path(a.out), a.ads)
    stats = site.build()
    size = sum(f.stat().st_size for f in Path(a.out).rglob('*') if f.is_file())
    stats.update(build_seconds=round(time.time() - t0, 1), total_mb=round(size / 1e6, 1))
    print(json.dumps(stats, indent=2))


if __name__ == '__main__':
    main()
