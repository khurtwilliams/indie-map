#!/usr/bin/env python3
"""Generates the per-site /[DOMAIN].json files served on map.snarfed.org.

Usage: make_site_files.py sites.json[.gz] social_graph_links.json[.gz]

where sites.json[.gz] is generated by sites_to_bigquery.py, and
social_graph_links.json[.gz] is this table:
https://bigquery.cloud.google.com/table/indie-map:indiemap.social_graph_links
created by the query 'Social graph: links by mf2, outbound':
https://bigquery.cloud.google.com/savedquery/464705913036:c1dce91deaed46a5bcf3452e5b781542

TODO:
- option to limit to input domains
- truncate at 1k (?), include total number
"""
from collections import defaultdict, OrderedDict
import copy
import decimal
from decimal import Decimal
import gzip
from itertools import chain
import operator
import os
# simplejson supports encoding Decimal, but json doesn't
import simplejson as json
import sys

MF2_WEIGHTS = {
    'in-reply-to': 1,
    'invitee': 1,
    'quotation-of': .6,
    'repost-of': .6,
    'like-of': .4,
    'favorite-of': .4,
    'bookmark-of': .4,
    'other': .2,
}
DIRECTION_WEIGHTS = {
    'out': 1,
    'in': .5,
}
MAX_BASE_LINKS = 1000  # cap on number of link domains in base files

decimal.getcontext().prec = 3  # calculate/output scores at limited precision

# currently unused
with open('../crawl/domain_blacklist.txt', 'rt', encoding='utf-8') as f:
    BLACKLIST = frozenset((line.strip() for line in f if line.strip()))


def load_links(links_in):
    """Loads and processes a social graph links JSON file.

    Args:
      links_in: sequence of social graph links objects. See file docstring.

    Returns: (links, domains, out_counts, in_counts)

    links:
      {'[DOMAIN]': {
          'links_out': [INTEGER],
          'links_in':  [INTEGER],
          'links': {
            'TO_DOMAIN': {
              'out': {
                'in-reply-to': [INTEGER],  # mf2 classes
                'like-of': [INTEGER],
                ...
                'other': [INTEGER],
              },
              'in': {
                [SAME]
              },
              'score': [FLOAT],
            },
            ...
          },
        },
        ...,
      }
    domains: set of from domains
    out_counts, in_counts: {'[DOMAIN]': [INTEGER]}
    """
    print('Loading', end='')

    links = defaultdict(lambda: defaultdict(lambda: defaultdict(
        lambda: defaultdict(int))))
    out_counts = defaultdict(int)
    in_counts = defaultdict(int)
    from_domains = set()

    for i, link in enumerate(links_in):
        if i and i % 10000 == 0:
            print('.', end='', flush=True)

        from_domain = link['from_domain']
        from_domains.add(from_domain)
        to_domain = link['to_domain']
        num = int(link['num'])
        mf2 = link.get('mf2_class', 'other')
        if mf2.startswith('u-'):
            mf2 = mf2[2:]

        links[from_domain][to_domain]['out'][mf2] += num
        links[to_domain][from_domain]['in'][mf2] += num
        out_counts[from_domain] += num
        in_counts[to_domain] += num

    return links, from_domains, out_counts, in_counts


def make_full(sites, single_links):
    """Generates and returns output site objects with all link domains.

    Args:
      sites: sequence of input site objects
      links_file: sequence of link collections returned by load_links()

    Returns:
      generator of output JSON site objects
    """
    links, from_domains, out_counts, in_counts = load_links(single_links)

    # calculate scores
    print('\nScoring', end='')
    for i, domains in enumerate(links.values()):
        if i and i % 10000 == 0:
            print('.', end='', flush=True)

        max_score = 0
        for stats in domains.values():
            score = 0
            for direction, counts in stats.items():
                for mf2, count in counts.items():
                    score += Decimal(count * MF2_WEIGHTS[mf2] *
                                     DIRECTION_WEIGHTS[direction])
            stats['score'] = score
            if score > max_score:
                max_score = score

        # normalize scores to (0, 1] per domain
        for stats in domains.values():
            stats['score'] /= max_score

    # emit each site
    print('\nGenerating full', end='')
    extra_sites = [{'domain': domain} for domain in
                   from_domains - set(site['domain'] for site in sites)]

    for i, site in enumerate(sites + tuple(extra_sites)):
        if i and i % 10 == 0:
            print('.', end='', flush=True)
        domain = site['domain']
        domain_links = links.get(domain, {})
        site.update({
            'hcard': json.loads(site.get('hcard', '{}')) or {},
            'links_out': out_counts.get(domain),
            'links_in': in_counts.get(domain),
            'links': OrderedDict(sorted(domain_links.items(),
                                        key=lambda item: item[1]['score'],
                                        reverse=True)),
        })
        site.pop('mf2', None)
        site.pop('html', None)
        yield site

    print()


def make_base(full):
    """Generates and returns output sites with capped number of link domains.

    Number of link domains per site is capped at MAX_BASE_LINKS.

    Args:
      full: sequence of full site objects created by make_full()

    Returns:
      sequence of output JSON site objects
    """
    print('\nGenerating base', end='')

    base = copy.deepcopy(full)
    for i, site in enumerate(base):
        if i and i % 10 == 0:
            print('.', end='', flush=True)
        if len(site['links']) > MAX_BASE_LINKS:
            site['links'] = OrderedDict(list(site['links'].items())[:MAX_BASE_LINKS])
            site['links_truncated'] = True

    return base


def make_internal(sites, full):
    """Generates and returns output JSON objects with links to internal domains.

    Internal domains are just the sites in the dataset itself.

    Args:
      full: sequence of full site objects created by make_full()

    Returns:
      sequence of output JSON site objects
    """
    print('\nGenerating internal', end='')

    our_domains = set(s['domain'] for s in sites + full)
    internal = copy.deepcopy(full)

    for i, site in enumerate(internal):
        if i and i % 10 == 0:
            print('.', end='', flush=True)
        site['links'] = OrderedDict(
            (domain, val) for domain, val in site['links'].items()
            if domain in our_domains)

    return internal


def open_fn(path, mode):
    return (gzip.open if path.endswith('.gz') else open)(
        path, mode, encoding='utf-8')


def json_dump(dir, objs):
    for obj in objs:
        with open('%s/%s.json' % (dir, obj['domain']), 'wt', encoding='utf-8') as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)


if __name__ == '__main__':
    with open_fn(sys.argv[1], 'rt') as f:
         sites = [json.loads(line) for line in f]

    with open_fn(sys.argv[2], 'rt') as f:
        links = [json.loads(line) for line in f]

    json_dump('full', make_full(sites, links))
    json_dump('base', make_base(sites, links))
    json_dump('internal', make_internal(sites, links))
