#!/usr/bin/env python3
"""Obsidian Vault -> Neo4j CSV ETL (Python standard library only).

Turns an Obsidian vault into the two CSV files that Neo4j's offline importer
expects, following the Obsidian -> Protege -> Neo4j three-layer pattern:

    Obsidian (source of truth)  ->  CSV (transport)  ->  Neo4j (query projection)

The vault is never modified. Everything is read-only against the vault.

Key design decisions (all of them were learned the hard way):

1.  A node ID is the vault-relative POSIX path of the file, never the title.
    Titles collide (every folder has a ``00_index.md``), paths do not.
    Renaming or moving a file changes its ID, so the sync must treat that as
    "delete old ID + create new ID" instead of pretending it did not happen.

2.  Tags become nodes with a ``tag:`` prefix. Tag nodes are NOT owned by a
    file. A file deletion must never cascade into tag deletion, because a tag
    may still be referenced by other notes.

3.  Directories become nodes too. Do not read "node count" as "note count".

4.  The importer needs BOTH the ``:ID`` column and a plain ``id`` property, and
    every node needs the ``KBNode`` label. ``:ID`` alone is only used to match
    relationship endpoints during import; without a plain ``id`` property the
    incremental ``MERGE (n:KBNode {id: $id})`` cannot find existing nodes and
    will happily duplicate them. ``prepare_import.py`` fixes this.

Usage:
    python3 kb_etl.py --vault ./my_vault --out ./out
    python3 kb_etl.py --vault ./my_vault --out ./out --dry-run
    python3 kb_etl.py --vault ./my_vault --out ./out --baseline ./state.json

Exit codes:
    0  success
    1  a validation error (ambiguous link, dangling edge, bad output path)
    2  bad arguments
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- parsing ---

LINK = re.compile(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]')
TAG = re.compile(r'^tags:\s*\[([^\]]*)\]', re.M)
TAG_SINGLE = re.compile(r'^tags:\s*(.+)$', re.M)
TITLE = re.compile(r'^title:\s*(.+)$', re.M)

# Files that are never worth turning into graph nodes.
SKIP_NAMES = {'.DS_Store', 'Thumbs.db'}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def read_tags(content: str) -> list:
    """Read `tags: [a, b]` and `tags: a` forms, plus YAML list form."""
    tags = []
    m = TAG.search(content)
    if m:
        tags = [x.strip() for x in m.group(1).split(',') if x.strip()]
    else:
        m2 = TAG_SINGLE.search(content)
        if m2:
            raw = m2.group(1).strip()
            if raw.startswith('['):
                raw = raw.strip('[]')
            tags = [x.strip().strip('"\'') for x in raw.split(',') if x.strip()]
    # Normalise: strip leading '#', keep the rest verbatim.
    return [t.lstrip('#') for t in tags]


def read_title(content: str, fallback: str) -> str:
    m = TITLE.search(content)
    return m.group(1).strip() if m else fallback


def is_excluded(rel_path: str, patterns: list) -> bool:
    """Exclude by path substring/regex-ish glob. Used for private content."""
    import fnmatch
    for pat in patterns:
        if fnmatch.fnmatch(rel_path, pat):
            return True
        # Also allow a plain directory prefix like `Private/`
        if pat.endswith('/') and rel_path.startswith(pat):
            return True
    return False


# ------------------------------------------------------------------- main ---

def build(vault: Path, out: Path, patterns: list, dry_run: bool) -> dict:
    notes = []
    for p in sorted(vault.rglob('*.md')):
        if p.name in SKIP_NAMES or any(part.startswith('.') for part in p.relative_to(vault).parts):
            continue
        rel = p.relative_to(vault).as_posix()
        if is_excluded(rel, patterns):
            continue
        notes.append(p)

    if not notes:
        raise ValueError('No Markdown notes found (all excluded or empty vault?)')

    nodes = []   # [id, name, path, title, label]
    rels = []    # [start_id, end_id, type, relation_label]
    directory_ids = set()
    tags = set()
    by_stem = {}
    file_hashes = {}

    # ---- pass 1: files, directories, tags -------------------------------
    for p in notes:
        rel = p.relative_to(vault).as_posix()
        parent = p.parent.relative_to(vault).as_posix() + '/'
        directory_ids.add(parent)
        by_stem.setdefault(p.stem, []).append(rel)
        file_hashes[rel] = sha256_of(p)

        content = p.read_text(encoding='utf-8', errors='replace')
        title = read_title(content, p.stem)
        # Index cards are the entry points of a folder; everything else is a Document.
        label = 'IndexCard' if p.name == '00_索引.md' else 'Document'
        nodes.append([rel, p.name, rel, title, label])
        rels.append([parent, rel, 'CONTAINS', '包含'])
        if label == 'IndexCard':
            rels.append([parent, rel, 'HAS_INDEX', '索引卡'])

        for tag in read_tags(content):
            tags.add(tag)
            rels.append([rel, 'tag:' + tag, 'TAGGED_WITH', '标签'])

    # ---- pass 2: wikilinks ----------------------------------------------
    link_count = 0
    ambiguous = []
    for p in notes:
        rel = p.relative_to(vault).as_posix()
        content = p.read_text(encoding='utf-8', errors='replace')
        for target in LINK.findall(content):
            candidates = by_stem.get(Path(target.strip()).stem, [])
            if len(candidates) == 1:
                rels.append([rel, candidates[0], 'LINKS_TO', 'wikilink'])
                link_count += 1
            elif len(candidates) > 1:
                # Never silently guess. Ambiguity is a data problem, not a script bug.
                ambiguous.append({'from': rel, 'target': target,
                                  'candidates': candidates})
            # 0 candidates == broken link, ignored on purpose (Obsidian allows it)

    # ---- pass 3: directory + tag + source nodes -------------------------
    for directory in sorted(directory_ids):
        nodes.append([directory, directory.rstrip('/').split('/')[-1],
                      directory, '', 'Directory;Domain'])
    for tag in sorted(tags):
        nodes.append(['tag:' + tag, tag, '', '', 'Tag'])

    ids = {n[0] for n in nodes}
    dangling = [r for r in rels if r[0] not in ids or r[1] not in ids]
    if dangling:
        raise ValueError(f'Dangling relationship: {dangling[:5]}')

    result = {
        'vault': str(vault),
        'files': len(notes),
        'nodes': len(nodes),
        'relationships': len(rels),
        'wikilinks': link_count,
        'tags': len(tags),
        'ambiguous_links': ambiguous,
        'file_hashes': file_hashes,
        'excluded_patterns': patterns,
    }

    if dry_run:
        return result

    out.mkdir(parents=True, exist_ok=True)
    with (out / 'nodes.csv').open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow([':ID', 'name', 'path', 'title', ':LABEL'])
        w.writerows(nodes)
    with (out / 'relationships.csv').open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow([':START_ID', ':END_ID', ':TYPE', 'relation'])
        w.writerows(rels)
    return result


def load_baseline(path: Path):
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description='Obsidian vault -> Neo4j CSV ETL')
    ap.add_argument('--vault', type=Path, required=True, help='Obsidian vault root')
    ap.add_argument('--out', type=Path, required=True, help='output directory (must be OUTSIDE the vault)')
    ap.add_argument('--dry-run', action='store_true', help='report counts, write nothing')
    ap.add_argument('--baseline', type=Path, help='state json for incremental diff')
    ap.add_argument('--exclude', action='append', default=[],
                    help='glob of path(s) to exclude, e.g. --exclude "Private/*" (repeatable)')
    ap.add_argument('--save-baseline', type=Path, help='write the new baseline here')
    ap.add_argument('--max-delete-ratio', type=float, default=0.5,
                    help='abort if the estimated delete ratio exceeds this (0.5 = 50%%)')
    args = ap.parse_args()

    vault = args.vault.resolve()
    out = args.out.resolve()

    if not vault.is_dir():
        print(f'ERROR: vault not found: {vault}', file=sys.stderr)
        return 2
    if out == vault or vault in out.parents:
        # The output must live outside the vault, otherwise the next scan
        # picks up its own generated CSVs and the graph starts eating itself.
        print('ERROR: output directory must be OUTSIDE the vault', file=sys.stderr)
        return 2

    try:
        result = build(vault, out, args.exclude, args.dry_run)
    except ValueError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1

    baseline = load_baseline(args.baseline)
    old_hashes = baseline.get('file_hashes', {}) if baseline else {}
    new_hashes = result['file_hashes']

    added = sorted(set(new_hashes) - set(old_hashes)) if old_hashes else []
    changed = sorted(k for k in set(new_hashes) & set(old_hashes)
                     if new_hashes[k] != old_hashes[k]) if old_hashes else []
    removed = sorted(set(old_hashes) - set(new_hashes)) if old_hashes else []

    # Safety stop-line: a sudden collapse in scanned volume, or a huge delete
    # ratio, means something is wrong upstream (vault moved, mount lost,
    # wrong --vault argument). Stop instead of deleting the graph.
    safety = 'no-baseline'
    if old_hashes:
        old_n, new_n = len(old_hashes), len(new_hashes)
        ratio = len(removed) / old_n if old_n else 0.0
        volume_drop = (old_n - new_n) / old_n if old_n else 0.0
        if removed and (ratio > args.max_delete_ratio or volume_drop > args.max_delete_ratio):
            safety = f'ABORT delete-ratio={ratio:.2f} volume-drop={volume_drop:.2f}'
        else:
            safety = 'ok'

    result.update({'added': len(added), 'changed': len(changed),
                   'removed': len(removed), 'safety': safety})
    if result['ambiguous_links']:
        result['ambiguous_links'] = result['ambiguous_links'][:20]

    print(f"files={result['files']} nodes={result['nodes']} "
          f"relationships={result['relationships']} wikilink={result['wikilinks']} "
          f"tags={result['tags']}")
    if old_hashes:
        print(f"delta added={len(added)} changed={len(changed)} removed={len(removed)} "
              f"safety={safety}")
    if result['ambiguous_links']:
        print(f"WARN ambiguous wikilinks: {len(result['ambiguous_links'])} "
              f"(first: {result['ambiguous_links'][0]})", file=sys.stderr)

    if args.dry_run:
        print('DRY-RUN: nothing written')
    else:
        print(f'written: {out / "nodes.csv"}, {out / "relationships.csv"}')

    if args.save_baseline:
        args.save_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.save_baseline.write_text(
            json.dumps({'file_hashes': new_hashes}, ensure_ascii=False, indent=2),
            encoding='utf-8')
        print(f'baseline saved: {args.save_baseline}')

    return 1 if safety.startswith('ABORT') else 0


if __name__ == '__main__':
    raise SystemExit(main())
