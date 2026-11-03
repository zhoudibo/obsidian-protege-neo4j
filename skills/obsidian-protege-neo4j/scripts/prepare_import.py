#!/usr/bin/env python3
"""Turn raw ETL CSV into a Neo4j-offline-import-ready CSV pair.

This is the step that most people skip, and skipping it is the single most
common cause of "the first import worked but the incremental sync duplicates
everything or deletes the wrong nodes".

Why this step exists
--------------------
Neo4j's ``neo4j-admin database import full`` uses the ``:ID`` column to match
relationship endpoints. But ``:ID`` is an *import-time* artefact, not a stored
property. If your daily sync then does::

    MERGE (n:KBNode {id: $id})

there is no ``id`` property to match on, so every run creates NEW nodes instead
of updating the existing ones. The graph grows on every sync.

The fix is small and mechanical:

1.  keep the ``:ID`` column (the importer needs it), AND
2.  add a plain ``id`` property carrying the same value, AND
3.  give every node the ``KBNode`` label (so ``MATCH (n:KBNode)`` sees them).

Additionally it validates, before you touch a database:

*   node IDs are non-empty and unique
*   every relationship endpoint exists in the node table
*   the required columns are present

Usage:
    python3 prepare_import.py ./raw_import ./ready_import

Exit codes:
    0  success
    1  validation error (do NOT import)
    2  bad arguments
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

REQUIRED_NODE_FIELDS = (':ID', ':LABEL')
REQUIRED_REL_FIELDS = (':START_ID', ':END_ID', ':TYPE')
BASE_LABEL = 'KBNode'


def fail(msg: str) -> int:
    print(f'ERROR: {msg}', file=sys.stderr)
    return 1


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    if src.resolve() == dst.resolve():
        return fail('output must differ from input')
    if not (src / 'nodes.csv').exists() or not (src / 'relationships.csv').exists():
        return fail(f'{src} must contain nodes.csv and relationships.csv')

    dst.mkdir(parents=True, exist_ok=True)

    # ---- nodes -----------------------------------------------------------
    with (src / 'nodes.csv').open(newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        for need in REQUIRED_NODE_FIELDS:
            if need not in fields:
                return fail(f'nodes.csv missing required column {need}')
        rows = list(reader)

    ids = [(row.get(':ID') or '').strip() for row in rows]
    if not rows:
        return fail('nodes.csv is empty')
    if any(not i for i in ids):
        bad = [i + 2 for i, v in enumerate(ids) if not v][:10]
        return fail(f'node IDs missing on CSV rows {bad}')
    if len(ids) != len(set(ids)):
        seen, dup = set(), []
        for i, v in enumerate(ids):
            if v in seen:
                dup.append(f'{v} (row {i + 2})')
            seen.add(v)
        return fail(f'duplicate node IDs: {dup[:10]}')

    out_fields = [x for x in fields if x != 'id']
    out_fields.insert(out_fields.index(':ID') + 1, 'id')

    with (dst / 'nodes.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        for row in rows:
            row['id'] = row[':ID']
            labels = [x.strip() for x in row.get(':LABEL', '').split(';') if x.strip()]
            row[':LABEL'] = ';'.join(dict.fromkeys([BASE_LABEL, *labels]))
            writer.writerow({k: row.get(k, '') for k in out_fields})

    # ---- relationships ---------------------------------------------------
    with (src / 'relationships.csv').open(newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        rel_fields = reader.fieldnames or []
        for need in REQUIRED_REL_FIELDS:
            if need not in rel_fields:
                return fail(f'relationships.csv missing required column {need}')
        rels = list(reader)

    idset = set(ids)
    dangling = [i for i, row in enumerate(rels, 2)
                if (row.get(':START_ID') or '').strip() not in idset
                or (row.get(':END_ID') or '').strip() not in idset]
    if dangling:
        return fail(f'dangling relationship rows (CSV line numbers): {dangling[:10]}')

    with (dst / 'relationships.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=rel_fields)
        writer.writeheader()
        writer.writerows(rels)

    print(f'PASS nodes={len(rows)} relationships={len(rels)}; '
          f'id property and {BASE_LABEL} label present')
    print('Next: import these CSV files into a NEW EMPTY offline database only.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
