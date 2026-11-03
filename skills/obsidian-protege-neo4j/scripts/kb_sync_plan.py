#!/usr/bin/env python3
"""Plan a Neo4j graph sync without touching the database.

The rule that keeps a knowledge graph from losing data is simple and was
learned the expensive way: **you may not delete a node because "this run did
not scan it"**. A node that was not scanned might be a tag still referenced by
another note, a source-system node, or a node whose file was merely skipped
by a filter.

Correct ownership model:

    file/directory nodes  ->  owned by the vault, tracked by content hash
    tag nodes             ->  NOT owned by a file; only reclaimed when they
                              have zero incoming edges (orphan tags)
    source-system nodes   ->  owned by the pipeline, never auto-deleted

This script reads the vault and the current graph state (a JSON export of
``MATCH (n:KBNode) RETURN n.id, labels(n)``) and prints a plan:

    ADD     nodes that exist in the vault but not in the graph
    UPDATE  nodes whose content hash changed
    DELETE  file/directory nodes in the graph but no longer in the vault
    KEEP    everything else, explicitly listing orphan tags separately

It refuses to produce a destructive plan when the vault scan volume collapsed.

Usage:
    python3 kb_sync_plan.py --vault ./vault --graph-state ./graph_nodes.json
    python3 kb_sync_plan.py --vault ./vault --graph-state ./graph_nodes.json --json

Exit codes:
    0  plan generated
    1  safety stop (do not execute the delete step)
    2  bad arguments
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

LINK = re.compile(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]')
TAG = re.compile(r'^tags:\s*\[([^\]]*)\]', re.M)
TITLE = re.compile(r'^title:\s*(.+)$', re.M)

# Nodes whose lifecycle is NOT tied to a vault file.
NON_OWNED_PREFIXES = ('tag:', 'source:')


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scan_vault(vault: Path) -> dict:
    files, dirs, tags = {}, set(), set()
    for p in sorted(vault.rglob('*.md')):
        if any(part.startswith('.') for part in p.relative_to(vault).parts):
            continue
        rel = p.relative_to(vault).as_posix()
        files[rel] = sha256_of(p)
        dirs.add(p.parent.relative_to(vault).as_posix() + '/')
        content = p.read_text(encoding='utf-8', errors='replace')
        m = TAG.search(content)
        if m:
            for t in m.group(1).split(','):
                t = t.strip().lstrip('#')
                if t:
                    tags.add('tag:' + t)
    return {'files': files, 'dirs': dirs, 'tags': tags}


def load_graph_state(path: Path) -> list:
    """Expect a JSON list of {'id':..., 'labels':[...], 'hash':...} or a dict."""
    if not path.exists():
        print(f'ERROR: graph state file not found: {path}', file=sys.stderr)
        raise SystemExit(2)
    data = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(data, dict):
        data = data.get('nodes', [])
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description='Plan an incremental graph sync')
    ap.add_argument('--vault', type=Path, required=True)
    ap.add_argument('--graph-state', type=Path, required=True,
                    help='JSON export of current graph nodes')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--max-delete-ratio', type=float, default=0.3)
    args = ap.parse_args()

    if not args.vault.is_dir():
        print(f'ERROR: vault not found: {args.vault}', file=sys.stderr)
        return 2

    scanned = scan_vault(args.vault.resolve())
    graph = load_graph_state(args.graph_state)

    vault_ids = set(scanned['files']) | set(scanned['dirs']) | set(scanned['tags'])
    graph_ids = set()
    graph_hashes = {}
    graph_labels = {}
    for n in graph:
        nid = n.get('id') or n.get(':ID')
        if not nid:
            continue
        graph_ids.add(nid)
        graph_hashes[nid] = n.get('hash') or ''
        labels = n.get('labels') or []
        if isinstance(labels, str):
            labels = labels.split(';')
        graph_labels[nid] = labels

    add = sorted(vault_ids - graph_ids)

    update = []
    for fid, h in scanned['files'].items():
        old = graph_hashes.get(fid)
        if old is not None and old and old != h:
            update.append(fid)

    # Deletion candidates: only vault-owned nodes that vanished.
    delete_candidates = []
    orphan_tags = []
    for gid in graph_ids:
        if gid in vault_ids:
            continue
        if gid.startswith(NON_OWNED_PREFIXES):
            if gid.startswith('tag:'):
                orphan_tags.append(gid)
            continue
        delete_candidates.append(gid)

    # Safety stop-line.
    owned_graph = [g for g in graph_ids if not g.startswith(NON_OWNED_PREFIXES)]
    old_count = len(owned_graph)
    new_count = len(scanned['files']) + len(scanned['dirs'])
    delete_ratio = len(delete_candidates) / old_count if old_count else 0.0
    volume_drop = (old_count - new_count) / old_count if old_count else 0.0

    safety = 'ok'
    if delete_candidates and (delete_ratio > args.max_delete_ratio
                              or volume_drop > args.max_delete_ratio):
        safety = 'STOP'

    plan = {
        'scanned_files': len(scanned['files']),
        'scanned_dirs': len(scanned['dirs']),
        'scanned_tags': len(scanned['tags']),
        'graph_nodes': len(graph_ids),
        'add': len(add),
        'update': len(update),
        'delete_candidates': len(delete_candidates),
        'orphan_tags_not_auto_deleted': len(orphan_tags),
        'delete_ratio': round(delete_ratio, 4),
        'volume_drop': round(volume_drop, 4),
        'safety': safety,
        'add_ids': add[:50],
        'update_ids': update[:50],
        'delete_ids': delete_candidates[:50],
        'orphan_tag_ids': orphan_tags[:50],
    }

    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print(f"scanned: files={plan['scanned_files']} dirs={plan['scanned_dirs']} "
              f"tags={plan['scanned_tags']}")
        print(f"graph  : {plan['graph_nodes']} nodes")
        print(f"ADD    : {plan['add']}")
        print(f"UPDATE : {plan['update']}")
        print(f"DELETE : {plan['delete_candidates']} "
              f"(ratio={plan['delete_ratio']}, volume_drop={plan['volume_drop']})")
        print(f"TAGS   : {plan['orphan_tags_not_auto_deleted']} orphan tags kept "
              f"(not auto-deleted)")
        print(f"SAFETY : {plan['safety']}")
        if safety == 'STOP':
            print('STOP: do not execute deletions. Check the vault path, mounts, '
                  'and filters before retrying.', file=sys.stderr)

    return 1 if safety == 'STOP' else 0


if __name__ == '__main__':
    raise SystemExit(main())
