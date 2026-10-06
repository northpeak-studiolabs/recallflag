#!/usr/bin/env python3
"""Fetch US product recalls (FDA food/drug/device, CPSC, NHTSA) into data/recalls.json.

Standalone port of the `us-product-recalls` Apify actor (scraper-portfolio) with no
Apify dependency, so a GitHub Action can run it. Only needs `requests`.

Sources
  * openFDA enforcement reports  https://open.fda.gov/apis/food/enforcement/ (also drug, device)
  * CPSC Recalls REST API         https://www.saferproducts.gov/RestWebServices/Recall
  * NHTSA recalls dataset         https://data.transportation.gov/resource/6axg-epim.json
  * NHTSA flat file (make/model/year per campaign)
                                  https://static.nhtsa.gov/odi/ffdd/rcl/FLAT_RCL_POST_2010.zip

Politeness: one request at a time, a short pause between pages, exponential backoff on
429/5xx/network errors, and an on-disk response cache (data/cache, default TTL 20 h) so a
re-run on the same day does not hit the APIs again. Set OPENFDA_API_KEY to raise the
openFDA daily quota (not required: a full run makes ~15 openFDA requests).

Usage: python fetch.py [--days 730] [--ttl-hours 20] [--out data/recalls.json]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
UA = 'RecallWatchBot/0.1 (+static recall lookup site; contact via site footer)'
FDA_KINDS = {'fda-food': 'food', 'fda-drug': 'drug', 'fda-device': 'device'}
NHTSA_FLAT = 'https://static.nhtsa.gov/odi/ffdd/rcl/FLAT_RCL_POST_2010.zip'

STATES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas', 'CA': 'California', 'CO': 'Colorado',
    'CT': 'Connecticut', 'DE': 'Delaware', 'DC': 'District of Columbia', 'FL': 'Florida', 'GA': 'Georgia',
    'HI': 'Hawaii', 'ID': 'Idaho', 'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland', 'MA': 'Massachusetts',
    'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi', 'MO': 'Missouri', 'MT': 'Montana',
    'NE': 'Nebraska', 'NV': 'Nevada', 'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico',
    'NY': 'New York', 'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina', 'SD': 'South Dakota',
    'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah', 'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington',
    'WV': 'West Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming', 'PR': 'Puerto Rico',
}
# Two-letter codes that are also common English words; only accepted inside a list of codes.
AMBIGUOUS = {'IN', 'OR', 'ME', 'OK', 'HI', 'PA', 'MA', 'DE', 'LA', 'CO', 'ID', 'OH', 'AL'}
NATIONWIDE = re.compile(r'\b(nation[- ]?wide|nationally|all (50 )?states|throughout the (us|u\.s\.|united states)|'
                        r'distributed (in|throughout) the (us|u\.s\.|united states)|us nationwide)\b', re.I)

# Hazard tags: (tag, regex over reason/hazard text). Used for /hazard/<tag>/ hubs.
HAZARDS = [
    ('Listeria', r'listeria'), ('Salmonella', r'salmonella'), ('E. coli', r'e\.? ?coli|stec\b'),
    ('Botulism', r'botulin|clostridium'), ('Undeclared allergen', r'undeclared|allergen'),
    ('Foreign material', r'foreign (material|object|matter)|metal (fragment|piece|shaving)|plastic (fragment|piece)|glass (fragment|piece)'),
    ('Mold', r'\bmou?ld\b'),
    ('Lead', r'lead (poisoning|content|level|paint|limit|ban)|(contains?|containing|levels? of|amounts? of|excessive|high) lead\b|lead (exceed|in excess)'), ('Sterility', r'steril'),
    ('Microbial contamination', r'microbial|bacteria|burkholderia|contaminat'),
    ('Mislabeling', r'mislabel|label(l)?ing error|incorrect label|wrong label'),
    ('Potency', r'subpotent|superpotent|potency'), ('Manufacturing practices (cGMP)', r'cgmp|good manufacturing'),
    ('Software', r'software'), ('Fire', r'\bfire|flammab|ignit'), ('Burn', r'\bburn'),
    ('Choking', r'chok'), ('Fall', r'\bfall'), ('Laceration', r'lacerat|\bcut'),
    ('Electrical shock', r'shock|electrocut'), ('Strangulation', r'strangulat|entangle'),
    ('Entrapment', r'entrap'), ('Drowning', r'drown'), ('Tip-over', r'tip[- ]?over'),
    ('Poisoning', r'poison|ingest'), ('Crash risk', r'crash'), ('Injury risk', r'injur'),
]
HAZARD_RE = [(t, re.compile(p, re.I)) for t, p in HAZARDS]


def log(msg: str) -> None:
    print(f'[{dt.datetime.now():%H:%M:%S}] {msg}', file=sys.stderr, flush=True)


class Http:
    """requests.Session with retries, politeness delay and an on-disk cache."""

    def __init__(self, cache_dir: Path, ttl_hours: float, delay: float = 0.4):
        self.s = requests.Session()
        self.s.headers.update({'User-Agent': UA})
        self.cache_dir = cache_dir
        self.ttl = ttl_hours * 3600
        self.delay = delay
        self.hits = self.requests = 0
        cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, url: str, params: dict | None, ext: str) -> Path:
        key = url + '?' + json.dumps(params or {}, sort_keys=True)
        return self.cache_dir / (hashlib.sha1(key.encode()).hexdigest()[:20] + ext)

    def get(self, url: str, params: dict | None = None, *, binary: bool = False, allow_404: bool = False,
            retries: int = 5):
        path = self._cache_path(url, params, '.bin' if binary else '.json')
        if path.exists() and time.time() - path.stat().st_mtime < self.ttl:
            self.hits += 1
            return path.read_bytes() if binary else json.loads(path.read_text())
        wait = 2.0
        for attempt in range(retries + 1):
            try:
                self.requests += 1
                r = self.s.get(url, params=params, timeout=120)
                if allow_404 and r.status_code == 404:
                    data = None
                    break
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f'HTTP {r.status_code}', response=r)
                r.raise_for_status()
                data = r.content if binary else r.json()
                break
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
                if (status is not None and status < 500 and status != 429) or attempt == retries:
                    raise
                log(f'  {exc}; retry in {wait:.0f}s')
                time.sleep(wait)
                wait *= 2
        time.sleep(self.delay)
        if binary:
            path.write_bytes(data)
        else:
            path.write_text(json.dumps(data))
        return data


# ---------------------------------------------------------------- helpers

def clean(v) -> str | None:
    if v is None:
        return None
    v = re.sub(r'\s+', ' ', str(v)).strip()
    return v or None


def ymd(v: str | None) -> str | None:
    return f'{v[:4]}-{v[4:6]}-{v[6:8]}' if v and len(v) == 8 and v.isdigit() else None


def parse_states(text: str | None) -> tuple[list[str], bool]:
    if not text:
        return [], False
    found = set()
    for code in re.findall(r'\b([A-Z]{2})\b', text):
        if code in STATES:
            # 'IN', 'OR' ... only count when they sit next to another code or a comma list.
            if code in AMBIGUOUS and not re.search(rf'(,|\band\b|;|/)\s*{code}\b|\b{code}\s*(,|;|/|\band\b)', text):
                continue
            found.add(code)
    low = text.lower()
    for code, name in STATES.items():
        if re.search(rf'\b{re.escape(name.lower())}\b', low):
            if code == 'WA' and 'washington, d' in low and low.count('washington') == 1:
                continue
            if code == 'VA' and 'west virginia' in low and low.count('virginia') == low.count('west virginia'):
                continue
            found.add(code)
    return sorted(found), bool(NATIONWIDE.search(text))


def hazard_tags(*texts: str | None) -> list[str]:
    hay = ' '.join(t for t in texts if t)
    return [t for t, rx in HAZARD_RE if rx.search(hay)]


def firm_key(name: str | None) -> str | None:
    """Collapse 'Acme Foods, Inc.' / 'ACME FOODS INC' into one grouping key."""
    if not name:
        return None
    n = name.lower().replace('&', ' and ')
    n = re.sub(r'\b(d/?b/?a|dba)\b.*', '', n)
    n = re.sub(r'\s+-\s+.*$', '', n)
    n = re.sub(r',?\s+of\s+[a-z ]+$', '', n)
    n = re.sub(r'[^a-z0-9 ]+', ' ', n)
    n = re.sub(r'\b(inc|incorporated|llc|l l c|ltd|limited|corp|corporation|co|company|lp|l p|plc|usa|us|na|'
               r'north america|america|the)\b', ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n or None


def nice_firm(name: str | None) -> str | None:
    name = clean(name)
    if not name:
        return None
    m = re.search(r'\bd/?b/?a[./]?\s+([^,;]+)', name, re.I)
    if m:
        name = m.group(1).strip()
    name = re.sub(r',?\s+of\s+(China|Canada|Taiwan|Hong Kong|Vietnam|Mexico|Germany|Sweden|[A-Z][a-z]+)$', '', name)
    if name.isupper() and len(name) > 4:
        name = re.sub(r"[A-Za-z][A-Za-z']*", lambda m: m.group(0) if len(m.group(0)) <= 3 and m.group(0) in
                      {'LLC', 'USA', 'LP', 'BMW', 'GM', 'KIA', 'RV', 'MFG', 'CO'} else m.group(0).capitalize(), name)
    return name


# ---------------------------------------------------------------- FDA

def fetch_fda(http: Http, source: str, since: dt.date, until: dt.date) -> list[dict]:
    kind = FDA_KINDS[source]
    out, skip = [], 0
    rng = f'report_date:[{since:%Y%m%d} TO {until:%Y%m%d}]'
    key = os.environ.get('OPENFDA_API_KEY')
    while skip <= 25000:
        params = {'search': rng, 'sort': 'report_date:desc', 'limit': 1000, 'skip': skip}
        if key:
            params['api_key'] = key
        data = http.get(f'https://api.fda.gov/{kind}/enforcement.json', params, allow_404=True)
        results = (data or {}).get('results') or []
        for r in results:
            dist = clean(r.get('distribution_pattern'))
            states, nationwide = parse_states(dist)
            reason = clean(r.get('reason_for_recall'))
            product = clean(r.get('product_description'))
            out.append({
                'id': r.get('recall_number'),
                'source': source,
                'date': ymd(r.get('report_date')),
                'initiated': ymd(r.get('recall_initiation_date')),
                'title': None,
                'product': product,
                'brand': nice_firm(r.get('recalling_firm')),
                'firmAddress': ', '.join(filter(None, [clean(r.get('address_1')), clean(r.get('city')),
                                                       clean(r.get('state')), clean(r.get('country'))])) or None,
                'category': {'food': 'Food', 'drug': 'Drugs', 'device': 'Medical devices'}[kind],
                'subcategory': clean(r.get('classification')),  # Class I/II/III hubs per category
                'reason': reason,
                'hazard': None,
                'hazardTags': hazard_tags(reason),
                'classification': clean(r.get('classification')),
                'status': clean(r.get('status')),
                'distribution': dist,
                'states': states,
                'nationwide': nationwide,
                'remedy': None,
                'quantity': clean(r.get('product_quantity')),
                'codeInfo': clean(r.get('code_info'))[:1500] if r.get('code_info') else None,
                'initiatedBy': clean(r.get('voluntary_mandated')),
                'eventId': r.get('event_id'),
                'link': f"https://www.accessdata.fda.gov/scripts/ires/index.cfm?Event={r['event_id']}" if r.get('event_id') else None,
            })
        log(f'  {source}: +{len(results)} (skip={skip})')
        if len(results) < 1000:
            break
        skip += 1000
    return out


# ---------------------------------------------------------------- CPSC

CPSC_KINDS = [
    ('E-bikes & scooters', r'e-?bike|electric bicycle|scooter|hoverboard|e-?scooter'),
    ('Bicycles & outdoor', r'bicycle|\bbikes?\b|helmet|trampoline|swing|playground|kayak|grill'),
    ('Baby & kids products', r'infant|baby|bab(y|ies)|crib|bassinet|stroller|high ?chair|pacifier|teether|child(ren)?.s|sleeper|car seat|walker|bouncer'),
    ('Toys', r'\btoys?\b|doll|plush|puzzle|water bead|squish|play ?set|rattle'),
    ('Furniture', r'dresser|chest|cabinet|furniture|nightstand|bookcase|\bdesk|table|chair|sofa|bed frame|bunk bed|bed rail|wardrobe|shelf|tv stand'),
    ('Clothing & jewelry', r'sweatshirt|hoodie|pajama|garment|jacket|clothing|drawstring|necklace|bracelet|jewelry|shoes|boots'),
    ('Batteries, chargers & electronics', r'batter(y|ies)|power bank|charger|adapter|cable|lithium|power strip|extension cord|headphone|speaker|electronic'),
    ('Heaters & fireplaces', r'heater|fireplace|space heater|stove|heating'),
    ('Kitchen & appliances', r'blender|cooker|air fryer|kettle|toaster|oven|microwave|refrigerator|dishwasher|mixer|pressure cooker|coffee|appliance|knife|knives|cookware|mug|bottle|tumbler'),
    ('Home & garden', r'lamp|light|candle|mattress|blanket|pillow|rug|mirror|window|blind|ladder|lawn|mower|saw|tool|pressure washer|generator|chainsaw|trimmer'),
    ('Personal care & medicine packaging', r'child[- ]resistant|packaging|lotion|cosmetic|hair dryer|shaver|toothbrush|massage'),
]
CPSC_KIND_RE = [(k, re.compile(p, re.I)) for k, p in CPSC_KINDS]


def cpsc_kind(*texts: str | None) -> str:
    hay = ' '.join(t for t in texts if t)
    return next((k for k, rx in CPSC_KIND_RE if rx.search(hay)), 'Other consumer products')


def fetch_cpsc(http: Http, since: dt.date, until: dt.date) -> list[dict]:
    rows = http.get('https://www.saferproducts.gov/RestWebServices/Recall',
                    {'format': 'json', 'RecallDateStart': since.isoformat(), 'RecallDateEnd': until.isoformat()}) or []
    out = []
    for r in rows:
        products = r.get('Products') or []
        hazard = '; '.join(clean(h.get('Name')) for h in r.get('Hazards') or [] if clean(h.get('Name'))) or None
        companies = [c.get('Name') for k in ('Manufacturers', 'Importers', 'Distributors', 'Retailers')
                     for c in r.get(k) or [] if c.get('Name') and k != 'Retailers']
        title = clean(r.get('Title'))
        brand = None
        if title:
            m = re.match(r'^(.{2,80}?)\s+(Recalls?|Announces?|Expands?|Reannounces?)\b', title)
            if m:
                brand = m.group(1)
        brand = brand or (companies[0] if companies else None)
        sold = '; '.join(clean(c.get('Name')).removeprefix('Sold At: ').removeprefix('Sold Exclusively At: ')
                         for c in r.get('Retailers') or [] if clean(c.get('Name'))) or None
        states, nationwide = parse_states(sold)
        types = [clean(p.get('Type')) for p in products if clean(p.get('Type'))]
        out.append({
            'id': f"CPSC-{r.get('RecallNumber')}",
            'source': 'cpsc',
            'date': (r.get('RecallDate') or '')[:10] or None,
            'initiated': None,
            'title': title,
            'product': '; '.join(clean(p.get('Name')) for p in products if clean(p.get('Name'))) or None,
            'brand': nice_firm(brand),
            'firmAddress': None,
            'category': 'Consumer products',
            'subcategory': cpsc_kind(title, ' '.join(types), '; '.join(clean(p.get('Name')) or '' for p in products)),
            'productType': types[0] if types else None,
            'reason': clean(r.get('Description')),
            'hazard': hazard,
            'hazardTags': hazard_tags(hazard),
            'classification': None,
            'status': None,
            'distribution': sold,
            'states': states,
            'nationwide': nationwide or bool(sold and 'nationwide' in sold.lower()),
            'remedy': '; '.join(clean(x.get('Name')) for x in r.get('Remedies') or [] if clean(x.get('Name'))) or None,
            'remedyOptions': [o.get('Option') for o in r.get('RemedyOptions') or [] if o.get('Option')],
            'quantity': '; '.join(clean(p.get('NumberOfUnits')) for p in products if clean(p.get('NumberOfUnits'))) or None,
            'codeInfo': '; '.join(clean(p.get('Model')) for p in products if clean(p.get('Model'))) or None,
            'injuries': '; '.join(clean(i.get('Name')) for i in r.get('Injuries') or [] if clean(i.get('Name'))) or None,
            'consumerContact': clean(r.get('ConsumerContact')),
            'manufacturedIn': sorted({c.get('Country') for c in r.get('ManufacturerCountries') or [] if c.get('Country')}),
            'upcs': [u.get('UPC') for u in r.get('ProductUPCs') or [] if u.get('UPC')][:20],
            'image': next((i.get('URL') for i in r.get('Images') or [] if i.get('URL')), None),
            'link': r.get('URL'),
        })
    log(f'  cpsc: {len(out)}')
    return out


# ---------------------------------------------------------------- NHTSA

def fetch_nhtsa(http: Http, since: dt.date, until: dt.date) -> list[dict]:
    out, offset = [], 0
    while True:
        rows = http.get('https://data.transportation.gov/resource/6axg-epim.json', {
            '$where': f"report_received_date >= '{since}T00:00:00' AND report_received_date <= '{until}T23:59:59'",
            '$order': 'report_received_date DESC, nhtsa_id', '$limit': 1000, '$offset': offset,
        }) or []
        for r in rows:
            consequence = clean(r.get('consequence_summary'))
            comp = clean(r.get('component'))
            out.append({
                'id': r.get('nhtsa_id'),
                'source': 'nhtsa',
                'date': (r.get('report_received_date') or '')[:10] or None,
                'initiated': None,
                'title': clean(r.get('subject')),
                'product': comp.title() if comp else None,
                'brand': nice_firm(r.get('manufacturer')),
                'firmAddress': None,
                'category': 'Vehicles',
                'subcategory': (comp.split(':')[0].split(',')[0].strip().title() if comp else None),
                'recallType': clean(r.get('recall_type')),
                'reason': clean(r.get('defect_summary')),
                'hazard': consequence,
                'hazardTags': hazard_tags(consequence),
                'classification': None,
                'status': None,
                'distribution': None,
                'states': [],
                'nationwide': True,
                'remedy': clean(r.get('corrective_action')),
                'quantity': clean(r.get('potentially_affected')),
                'codeInfo': clean(r.get('mfr_campaign_number')),
                'doNotDrive': r.get('do_not_drive') == 'Yes',
                'parkOutside': r.get('fire_risk_when_parked') == 'Yes',
                'vehicles': [],
                'link': (r.get('recall_link') or {}).get('url') or f"https://www.nhtsa.gov/recalls?nhtsaId={r.get('nhtsa_id')}",
            })
        log(f'  nhtsa: +{len(rows)} (offset={offset})')
        if len(rows) < 1000:
            break
        offset += 1000
    return out


def attach_nhtsa_vehicles(http: Http, recalls: list[dict], since: dt.date) -> int:
    """Join make/model/year from NHTSA's flat file onto the recall records (by campaign number)."""
    by_id = {r['id']: r for r in recalls if r['source'] == 'nhtsa'}
    if not by_id:
        return 0
    try:
        blob = http.get(NHTSA_FLAT, binary=True)
    except Exception as exc:  # the site still builds without make/model hubs
        log(f'  NHTSA flat file unavailable ({exc}); skipping make/model join')
        return 0
    groups: dict[str, dict[tuple, set]] = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = z.namelist()[0]
        with z.open(name) as fh:
            for raw in io.TextIOWrapper(fh, encoding='latin-1', errors='replace'):
                f = raw.rstrip('\r\n').split('\t')
                if len(f) < 16 or f[1] not in by_id:
                    continue
                make, model, year = clean(f[2]), clean(f[3]), clean(f[4])
                if not make or not model:
                    continue
                groups.setdefault(f[1], {}).setdefault((make, model, f[10].strip()), set())
                if year and year != '9999':
                    groups[f[1]][(make, model, f[10].strip())].add(int(year))
    n = 0
    for camp, g in groups.items():
        by_id[camp]['vehicles'] = [{'make': mk, 'model': md, 'type': t, 'years': sorted(ys)}
                                   for (mk, md, t), ys in sorted(g.items())][:200]
        n += 1
    return n


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=730)
    ap.add_argument('--ttl-hours', type=float, default=20)
    ap.add_argument('--out', default=str(HERE / 'data' / 'recalls.json'))
    ap.add_argument('--sources', default='fda-food,fda-drug,fda-device,cpsc,nhtsa')
    a = ap.parse_args()

    until = dt.date.today()
    since = until - dt.timedelta(days=a.days)
    http = Http(HERE / 'data' / 'cache', a.ttl_hours)
    t0 = time.time()
    recalls: list[dict] = []
    failures = []
    for src in a.sources.split(','):
        log(f'Fetching {src} {since}..{until}')
        try:
            if src in FDA_KINDS:
                recalls += fetch_fda(http, src, since, until)
            elif src == 'cpsc':
                recalls += fetch_cpsc(http, since, until)
            elif src == 'nhtsa':
                recalls += fetch_nhtsa(http, since, until)
        except Exception as exc:
            failures.append(src)
            log(f'  FAILED {src}: {exc}')
    if any(r['source'] == 'nhtsa' for r in recalls):
        log('Joining NHTSA make/model/year from flat file')
        log(f'  vehicles attached to {attach_nhtsa_vehicles(http, recalls, since)} recalls')

    # De-duplicate, drop records without id/date, newest first.
    seen, final = set(), []
    for r in sorted(recalls, key=lambda x: (x.get('date') or '', x.get('id') or ''), reverse=True):
        if not r.get('id') or not r.get('date') or r['id'] in seen:
            continue
        seen.add(r['id'])
        final.append(r)

    out = Path(a.out)
    previous = None
    if out.exists():
        try:
            previous = json.loads(out.read_text())
        except Exception:
            previous = None
    # If a source failed today, keep yesterday's records for it rather than publishing a hole.
    if failures and previous:
        kept = [r for r in previous.get('recalls', []) if r['source'] in failures and r['id'] not in seen]
        final += kept
        final.sort(key=lambda x: (x.get('date') or '', x.get('id') or ''), reverse=True)
        log(f'Kept {len(kept)} previous records for failed sources {failures}')
    if not final:
        sys.exit('No recalls fetched; refusing to overwrite data file.')

    counts = {}
    for r in final:
        counts[r['source']] = counts.get(r['source'], 0) + 1
    payload = {
        'generated': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        'since': since.isoformat(), 'until': until.isoformat(),
        'counts': counts, 'failedSources': failures, 'recalls': final,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
    tmp.replace(out)
    log(f'Wrote {len(final)} recalls to {out} {counts} in {time.time() - t0:.1f}s '
        f'({http.requests} requests, {http.hits} cache hits)')
    if failures:
        sys.exit(2 if len(failures) == len(a.sources.split(',')) else 0)


if __name__ == '__main__':
    main()
