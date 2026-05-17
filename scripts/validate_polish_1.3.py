"""Validate that a new chunking pipeline preserves the relational logic of an old one.

The new pipeline is expected to be a *superset* (more chunks extracted), so
chunk_ids will have shifted. This script:

  1. Loads both .jsonl files.
  2. Matches chunks by exact text to build an old_id -> new_id mapping.
  3. Translates each old chunk's `implicit_context_chunks` and
     `explicit_context_chunks` lists through the mapping and compares them to
     the new chunk's context lists.
  4. Reports a clean summary plus any regressions where old relationships
     were lost.

A "regression" is *any* deviation between the old chunk's context list (after
translation through the id mapping) and the new chunk's context list. Both
missing items (refs the new pipeline lost) and extra items (refs the new
pipeline hallucinated or wrongly attached) count as regressions — context
lists must match exactly.

Exits 0 on PASS, 1 on FAIL.
"""

import json
from collections import defaultdict
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(add_completion=False)


# ----------------------------------------------------------------------------
# IO
# ----------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict]:
    chunks = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                chunks.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno}: invalid JSON ({e})") from e
    return chunks


def build_text_index(chunks: list[dict]) -> dict[str, list[str]]:
    """text -> ordered list of chunk_ids that contain that text (preserves doc order)."""
    idx: dict[str, list[str]] = defaultdict(list)
    for c in chunks:
        idx[c["chunk"]].append(c["chunk_id"])
    return idx


# ----------------------------------------------------------------------------
# Mapping
# ----------------------------------------------------------------------------

def build_mapping(
    old_chunks: list[dict],
    new_text_to_ids: dict[str, list[str]],
) -> tuple[dict[str, str], list[str], list[tuple[str, str]]]:
    """
    Build an old_chunk_id -> new_chunk_id mapping by matching exact chunk text.

    For texts that appear multiple times, map by document order: the 1st old
    occurrence maps to the 1st new occurrence, the 2nd to the 2nd, and so on.
    If the new pipeline has fewer copies of a duplicated text than the old one
    (uncommon), the extra old occurrences fall back to the first new id and
    are flagged as ambiguous.

    Returns (mapping, unmapped_old_ids, ambiguous_pairs).
    """
    mapping: dict[str, str] = {}
    unmapped: list[str] = []
    ambiguous: list[tuple[str, str]] = []
    consumed: dict[str, int] = defaultdict(int)

    for c in old_chunks:
        text = c["chunk"]
        old_id = c["chunk_id"]
        candidates = new_text_to_ids.get(text, [])
        if not candidates:
            unmapped.append(old_id)
            continue
        i = consumed[text]
        if i < len(candidates):
            mapping[old_id] = candidates[i]
            consumed[text] += 1
        else:
            # More duplicates in old than in new — fall back, flag as ambiguous.
            mapping[old_id] = candidates[0]
            ambiguous.append((old_id, candidates[0]))
    return mapping, unmapped, ambiguous


# ----------------------------------------------------------------------------
# Context validation
# ----------------------------------------------------------------------------

CONTEXT_FIELDS = ("implicit_context_chunks", "explicit_context_chunks")


def validate_contexts(
    old_chunks: list[dict],
    new_id_to_chunk: dict[str, dict],
    mapping: dict[str, str],
) -> tuple[dict[str, int], list[dict], list[dict]]:
    counts = {
        "total": 0,
        "exact": 0,
        "regression": 0,     # any deviation: missing refs OR extra refs
        "untranslatable": 0, # old ref points to a chunk missing from new
    }
    regressions: list[dict] = []
    untranslatable_records: list[dict] = []

    for old_c in old_chunks:
        old_id = old_c["chunk_id"]
        if old_id not in mapping:
            continue
        new_c = new_id_to_chunk[mapping[old_id]]

        for field in CONTEXT_FIELDS:
            counts["total"] += 1
            old_refs = old_c.get(field) or []
            new_refs = set(new_c.get(field) or [])

            translated: set[str] = set()
            missing_refs: list[str] = []
            for r in old_refs:
                if r in mapping:
                    translated.add(mapping[r])
                else:
                    missing_refs.append(r)

            if missing_refs:
                counts["untranslatable"] += 1
                untranslatable_records.append(
                    {"old_id": old_id, "field": field, "missing_refs": missing_refs}
                )

            if translated == new_refs:
                counts["exact"] += 1
            else:
                counts["regression"] += 1
                regressions.append({
                    "old_id": old_id,
                    "new_id": new_c["chunk_id"],
                    "field": field,
                    "expected": sorted(translated),
                    "actual": sorted(new_refs),
                    "lost": sorted(translated - new_refs),
                    "extra": sorted(new_refs - translated),
                })
    return counts, regressions, untranslatable_records


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------

def _truncate(s: str, n: int = 90) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _section(title: str) -> None:
    print()
    print(title)


def _count_with_context(chunks: list[dict], field: str) -> int:
    return sum(1 for c in chunks if c.get(field))


def print_report(
    old_file: Path,
    new_file: Path,
    old_chunks: list[dict],
    new_chunks: list[dict],
    mapping: dict[str, str],
    unmapped: list[str],
    ambiguous: list[tuple[str, str]],
    old_dup_texts: dict[str, list[str]],
    new_dup_texts: dict[str, list[str]],
    counts: dict[str, int],
    regressions: list[dict],
    untranslatable: list[dict],
    show_examples: int,
) -> None:
    bar = "=" * 80
    print(bar)
    print("PIPELINE VALIDATION REPORT")
    print(bar)

    _section("SOURCES")
    print(f"  old: {old_file}")
    print(f"  new: {new_file}")

    _section("CHUNK COUNTS")
    old_total = len(old_chunks)
    new_total = len(new_chunks)
    old_impl = _count_with_context(old_chunks, "implicit_context_chunks")
    new_impl = _count_with_context(new_chunks, "implicit_context_chunks")
    old_expl = _count_with_context(old_chunks, "explicit_context_chunks")
    new_expl = _count_with_context(new_chunks, "explicit_context_chunks")
    rows = [
        ("total chunks",          old_total, new_total),
        ("with implicit context", old_impl,  new_impl),
        ("with explicit context", old_expl,  new_expl),
    ]
    print(f"  {'':<24} {'old':>6} {'new':>6} {'delta':>8}")
    for label, old_n, new_n in rows:
        delta = new_n - old_n
        print(f"  {label:<24} {old_n:>6} {new_n:>6} {delta:>+8d}")

    _section("CHUNK TEXT MATCHING")
    pct = 100.0 * len(mapping) / len(old_chunks) if old_chunks else 0.0
    print(f"  mapped (text found in new):   {len(mapping):>5} / {len(old_chunks)}  ({pct:.1f}%)")
    print(f"  missing from new:             {len(unmapped):>5}")
    print(f"  duplicate texts in old:       {len(old_dup_texts):>5}")
    print(f"  duplicate texts in new:       {len(new_dup_texts):>5}")
    print(f"  ambiguous mappings:           {len(ambiguous):>5}")

    _section("CONTEXT RELATIONSHIP VALIDATION")
    print(f"  relationships checked:        {counts['total']:>5}  ({len(mapping)} chunks × {len(CONTEXT_FIELDS)} fields)")
    print(f"    exact match:                {counts['exact']:>5}")
    print(f"    REGRESSIONS:                {counts['regression']:>5}")
    print(f"    untranslatable refs:        {counts['untranslatable']:>5}")

    # Detail sections
    if unmapped:
        _section(f"MISSING CHUNKS  (text in old, not in new)  — up to {show_examples} shown")
        old_by_id = {c["chunk_id"]: c for c in old_chunks}
        for oid in unmapped[:show_examples]:
            txt = old_by_id[oid]["chunk"]
            print(f"  {oid}: {_truncate(txt)!r}")
        if len(unmapped) > show_examples:
            print(f"  … and {len(unmapped) - show_examples} more")

    if ambiguous:
        _section(f"AMBIGUOUS MAPPINGS  — up to {show_examples} shown")
        for old_id, new_id in ambiguous[:show_examples]:
            print(f"  {old_id} → {new_id}  (more duplicates of this text in old than new)")
        if len(ambiguous) > show_examples:
            print(f"  … and {len(ambiguous) - show_examples} more")

    if regressions:
        _section(f"REGRESSIONS  — up to {show_examples} shown")
        for r in regressions[:show_examples]:
            print(f"  {r['old_id']} → {r['new_id']}  ({r['field']})")
            print(f"    expected (old, translated): {r['expected']}")
            print(f"    actual   (in new):          {r['actual']}")
            if r["lost"]:
                print(f"    lost  (in old, missing in new):  {r['lost']}")
            if r["extra"]:
                print(f"    extra (in new, absent in old):   {r['extra']}")
        if len(regressions) > show_examples:
            print(f"  … and {len(regressions) - show_examples} more")

    if untranslatable:
        _section(f"UNTRANSLATABLE REFS  — up to {show_examples} shown")
        for u in untranslatable[:show_examples]:
            print(f"  {u['old_id']}.{u['field']}: refs to missing {u['missing_refs']}")
        if len(untranslatable) > show_examples:
            print(f"  … and {len(untranslatable) - show_examples} more")

    # Verdict
    print()
    print(bar)
    is_pass = (
        len(unmapped) == 0
        and counts["regression"] == 0
        and counts["untranslatable"] == 0
    )
    if is_pass:
        print("VERDICT: PASS  — old pipeline behaviour preserved")
    else:
        print("VERDICT: FAIL  — see details above")
    print(bar)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

@app.command()
def validate(
    old_file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                    help="Path to the old pipeline's .jsonl output"),
    new_file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                    help="Path to the new pipeline's .jsonl output"),
    show_examples: int = typer.Option(
        5, "--show", "-n", help="Max examples to print per failure category."
    ),
    dump_mapping: Optional[Path] = typer.Option(
        None, "--dump-mapping", help="Optional path to write old_id -> new_id mapping as JSON."
    ),
) -> None:
    """Compare old vs new chunk JSONL pipelines for behavioural equivalence."""
    old_chunks = load_jsonl(old_file)
    new_chunks = load_jsonl(new_file)

    new_text_index = build_text_index(new_chunks)
    old_text_index = build_text_index(old_chunks)
    new_id_to_chunk = {c["chunk_id"]: c for c in new_chunks}

    old_dup_texts = {t: ids for t, ids in old_text_index.items() if len(ids) > 1}
    new_dup_texts = {t: ids for t, ids in new_text_index.items() if len(ids) > 1}

    mapping, unmapped, ambiguous = build_mapping(old_chunks, new_text_index)

    counts, regressions, untranslatable = validate_contexts(
        old_chunks, new_id_to_chunk, mapping
    )

    print_report(
        old_file=old_file, new_file=new_file,
        old_chunks=old_chunks, new_chunks=new_chunks,
        mapping=mapping, unmapped=unmapped, ambiguous=ambiguous,
        old_dup_texts=old_dup_texts, new_dup_texts=new_dup_texts,
        counts=counts, regressions=regressions, untranslatable=untranslatable,
        show_examples=show_examples,
    )

    if dump_mapping is not None:
        dump_mapping.parent.mkdir(parents=True, exist_ok=True)
        with open(dump_mapping, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"\nWrote mapping ({len(mapping)} entries) to {dump_mapping}")

    is_pass = (
        len(unmapped) == 0
        and counts["regression"] == 0
        and counts["untranslatable"] == 0
    )
    raise typer.Exit(code=0 if is_pass else 1)


if __name__ == "__main__":
    app()
