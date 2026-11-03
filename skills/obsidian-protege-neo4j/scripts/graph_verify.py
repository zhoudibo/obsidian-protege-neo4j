#!/usr/bin/env python3
"""Read-only verification of a Neo4j JSON export and a Git bundle.

The distinction this script exists to enforce:

    "the backup file is present and structurally valid"   !=
    "the backup restores into a working graph"

Level A (this script)  -> export integrity: node IDs unique, relationship
                          endpoints resolvable, counts consistent, bundle opens.
Level B (NOT this)     -> disaster recovery: import the backup into an
                          isolated Neo4j instance, run the fixed queries, and
                          compare the numbers. That needs a real database.

Never report Level B as done when only Level A was performed.

Usage:
    python3 graph_verify.py --export ./neo4j.export.json
    python3 graph_verify.py --export ./neo4j.export.json --bundle ./vault.git.bundle

Exit codes:
    0  verification passed
    1  verification failed
    2  bad arguments
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def verify_export(path: Path) -> list:
    problems = []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        return [f'cannot read/parse export: {e}']

    if isinstance(data, list):
        nodes = [x for x in data if isinstance(x, dict)]
        rels = []
    elif isinstance(data, dict):
        nodes = data.get('nodes', [])
        rels = data.get('relationships', data.get('edges', []))
    else:
        return ['unrecognised export structure']

    ids = []
    for n in nodes:
        nid = n.get('id') or n.get(':ID') or n.get('nodeId')
        if not nid:
            problems.append('node without id')
            continue
        ids.append(nid)
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        problems.append(f'duplicate node ids: {sorted(dupes)[:10]}')

    idset = set(ids)
    dangling = 0
    for r in rels:
        s = r.get('start') or r.get(':START_ID') or r.get('startId')
        e = r.get('end') or r.get(':END_ID') or r.get('endId')
        if s not in idset or e not in idset:
            dangling += 1
    if dangling:
        problems.append(f'{dangling} relationship(s) with unresolvable endpoints')

    if not nodes:
        problems.append('export contains no nodes')

    return problems


def verify_bundle(path: Path) -> list:
    problems = []
    try:
        res = subprocess.run(['git', 'bundle', 'verify', str(path)],
                             capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return [f'cannot run git bundle verify: {e}']
    out = (res.stdout + res.stderr).strip()
    if res.returncode != 0:
        problems.append(f'git bundle verify failed: {out[:300]}')
    elif 'is okay' not in out and 'complete history' not in out:
        problems.append(f'unexpected bundle verify output: {out[:300]}')
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description='Verify graph export and git bundle (read-only)')
    ap.add_argument('--export', type=Path, required=True)
    ap.add_argument('--bundle', type=Path)
    args = ap.parse_args()

    if not args.export.exists():
        print(f'ERROR: export not found: {args.export}', file=sys.stderr)
        return 2

    problems = verify_export(args.export)
    print(f'[EXPORT] {args.export.name}: '
          f'{"OK" if not problems else "PROBLEMS"}')
    for p in problems:
        print(f'  - {p}')

    if args.bundle:
        if not args.bundle.exists():
            print(f'[BUNDLE] not found: {args.bundle}')
            problems.append('bundle missing')
        else:
            bp = verify_bundle(args.bundle)
            print(f'[BUNDLE] {args.bundle.name}: '
                  f'{"OK" if not bp else "PROBLEMS"}')
            for p in bp:
                print(f'  - {p}')
            problems.extend(bp)
            if not bp:
                print('  note: a bundle only covers Git-TRACKED content. '
                      'Untracked attachments are NOT in the backup.')

    if problems:
        print()
        print('RESULT: FAILED')
        print('Level A (export integrity) did NOT pass.')
        print('This is still NOT a disaster-recovery test: Level B requires')
        print('importing the backup into an isolated Neo4j instance and')
        print('running the fixed node/relationship count queries.')
        return 1

    print()
    print('RESULT: PASSED (Level A -- export integrity only)')
    print('Level B (isolated restore + fixed queries) is NOT covered by this script.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
