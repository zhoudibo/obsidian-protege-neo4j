#!/usr/bin/env python3
"""Inspect an OWL 2 / Turtle ontology without opening Protege.

Protege is a desktop GUI. In practice you cannot paste a screenshot of it into
a report, and you cannot assert "the ontology has 13 classes" from a picture
either. So: model in Protege, then VERIFY the saved ``.owl`` / ``.ttl`` file
with this script and report the actual numbers.

It uses only the standard library, so it works on a machine with no rdflib and
no network. The parser is intentionally a pragmatic Turtle subset reader: it
extracts class declarations, object properties, data properties, labels and
declared domain/range. It is a *review aid*, not a W3C-compliant RDF parser.

For a full compliance check use a reasoner (HermiT / ELK) inside Protege, or
pyshacl for the SHACL shapes.

Usage:
    python3 owl_check.py ./ontology.ttl
    python3 owl_check.py ./ontology.ttl --json

Exit codes:
    0  ontology parsed
    1  file missing / unreadable / no classes found
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PREFIX = re.compile(r'^@prefix\s+([A-Za-z0-9_\-]*):\s*<([^>]*)>\s*\.\s*$', re.M)
SUBJECT = re.compile(r'^([A-Za-z0-9_\-]+:[A-Za-z0-9_\-]+)\s+(.*)$')
A = re.compile(r'\ba\s+owl:Class\b')
A_PROP = re.compile(r'\ba\s+owl:(ObjectProperty|DatatypeProperty)\b')
SUBCLASS = re.compile(r'rdfs:subClassOf\s+([A-Za-z0-9_\-]+:[A-Za-z0-9_\-]+)')
LABEL = re.compile(r'rdfs:label\s+"([^"]*)"')
DOMAIN = re.compile(r'rdfs:domain\s+([A-Za-z0-9_\-]+:[A-Za-z0-9_\-]+)')
RANGE = re.compile(r'rdfs:range\s+([A-Za-z0-9_\-]+:[A-Za-z0-9_\-]+)')
INVERSE = re.compile(r'owl:inverseOf\s+([A-Za-z0-9_\-]+:[A-Za-z0-9_\-]+)')


def strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        # Remove trailing comments that are not inside a quoted label.
        in_quote = False
        cut = len(line)
        for i, ch in enumerate(line):
            if ch == '"':
                in_quote = not in_quote
            elif ch == '#' and not in_quote:
                cut = i
                break
        out.append(line[:cut])
    return '\n'.join(out)


def split_statements(text: str) -> list:
    """Split Turtle text into statements ending with '.'.

    A '.' inside a quoted literal is not a terminator, so we track quotes.
    """
    statements = []
    buf = []
    in_quote = False
    for ch in text:
        if ch == '"':
            in_quote = not in_quote
            buf.append(ch)
            continue
        if ch == '.' and not in_quote:
            statements.append(''.join(buf))
            buf = []
            continue
        buf.append(ch)
    if ''.join(buf).strip():
        statements.append(''.join(buf))
    return [s.strip() for s in statements if s.strip()]


def parse(text: str) -> dict:
    text = strip_comments(text)
    prefixes = {m.group(1): m.group(2) for m in PREFIX.finditer(text)}

    # Each Turtle statement looks like:  subject  pred1 obj1 ; pred2 obj2 ; ...
    # We rebuild per-subject bodies by concatenating its predicate/object parts.
    blocks = []
    for statement in split_statements(text):
        flat = ' '.join(statement.split())
        # Drop a trailing '@prefix' / '@base' directive line.
        if flat.startswith('@prefix') or flat.startswith('@base'):
            continue
        # The subject is the first token, possibly an IRI in angle brackets.
        m = re.match(r'^(\S+)\s+(.*)$', flat)
        if not m:
            continue
        subject = m.group(1)
        body = m.group(2)
        # A statement may itself contain ';' separated predicate-object groups.
        # Keep the whole body: we only regex-scan for the predicates we care about.
        blocks.append((subject, body))

    classes, obj_props, data_props = [], [], []
    labels, domains, ranges, inverses = {}, {}, {}, {}
    subclass_map = {}

    for subject, body in blocks:
        body_flat = ' '.join(body.split())
        if A.search(body_flat):
            classes.append(subject)
        m = A_PROP.search(body_flat)
        if m:
            (obj_props if m.group(1) == 'ObjectProperty' else data_props).append(subject)
        lm = LABEL.search(body_flat)
        if lm:
            labels[subject] = lm.group(1)
        dm = DOMAIN.search(body_flat)
        if dm:
            domains[subject] = dm.group(1)
        rm = RANGE.search(body_flat)
        if rm:
            ranges[subject] = rm.group(1)
        im = INVERSE.search(body_flat)
        if im:
            inverses[subject] = im.group(1)
        sm = SUBCLASS.search(body_flat)
        if sm and subject not in classes:
            subclass_map[subject] = sm.group(1)

    # A class is really a class if it was declared as owl:Class OR it is used
    # as a subclass target of something that is.
    class_set = set(classes)
    for child, parent in subclass_map.items():
        if parent in class_set:
            class_set.add(child)

    prop_set = set(obj_props)
    for child, parent in subclass_map.items():
        if parent in prop_set:
            prop_set.add(child)

    return {
        'prefixes': prefixes,
        'classes': sorted(class_set),
        'object_properties': sorted(prop_set),
        'datatype_properties': sorted(data_props),
        'labels': labels,
        'domains': domains,
        'ranges': ranges,
        'inverses': inverses,
        'subclass_of': subclass_map,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='Inspect an OWL/Turtle ontology file')
    ap.add_argument('path', type=Path)
    ap.add_argument('--json', action='store_true', help='emit JSON instead of text')
    args = ap.parse_args()

    if not args.path.exists():
        print(f'ERROR: file not found: {args.path}', file=sys.stderr)
        return 1
    try:
        text = args.path.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        print(f'ERROR: cannot read {args.path}: {e}', file=sys.stderr)
        return 1

    info = parse(text)
    if not info['classes']:
        print('ERROR: no owl:Class found -- is this really an ontology file?',
              file=sys.stderr)
        return 1

    # Consistency smells that a human reviewer should look at.
    warnings = []
    for prop in info['object_properties']:
        if prop not in info['domains']:
            warnings.append(f'{prop}: no rdfs:domain declared')
        if prop not in info['ranges']:
            warnings.append(f'{prop}: no rdfs:range declared')
    for child, parent in info['subclass_of'].items():
        if child in info['classes'] and parent not in info['classes']:
            warnings.append(f'{child}: subclassOf unknown class {parent}')

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    print(f'file      : {args.path.name}')
    print(f'classes   : {len(info["classes"])}')
    for c in info['classes']:
        print(f'    {c}  ({info["labels"].get(c, "-")})')
    print(f'obj props : {len(info["object_properties"])}')
    for p in info['object_properties']:
        d = info['domains'].get(p, '-')
        r = info['ranges'].get(p, '-')
        inv = info['inverses'].get(p, '')
        extra = f'  inverseOf={inv}' if inv else ''
        print(f'    {p}: {d} -> {r}{extra}  ({info["labels"].get(p, "-")})')
    print(f'data props: {len(info["datatype_properties"])}')
    if warnings:
        print(f'WARNINGS ({len(warnings)}):')
        for w in warnings:
            print(f'    {w}')
    else:
        print('WARNINGS : none')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
