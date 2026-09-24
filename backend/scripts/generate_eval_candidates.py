"""
generate_eval_candidates.py
===========================
Part A of the LLM-assisted eval dataset pipeline.

What this script does
---------------------
1. Loads all drugs from the database, each with their FDA label text
   (drug_interactions / warnings / boxed_warning) via ingredient → fda_labels join.

2. True-positive candidate discovery (deterministic, no LLM):
   For each drug A, scans its FDA text for any mention of another drug B's brand
   name or generic name(s).  Each match → a candidate pair.

3. For each true-positive candidate, calls the same LLM used by the production
   pipeline (CHECK_INTERACTION_PROMPT, both drugs' full FDA text) to draft:
     - proposed_claim
     - proposed_citation
     - proposed_final_status  ("interaction_found" or "none_found")

4. True-negative candidates (deterministic, no LLM):
   Drug pairs with no name-mention overlap in either direction.
   Randomly sampled up to --max-negatives (default 20).
   proposed_final_status = "none_found", proposed_claim/citation = "".

5. Outputs eval_candidates_raw.csv with 17 columns.
   Last 5 columns (your_decision … notes) are blank — for the reviewer to fill.

Re-run safety
-------------
pair_id is deterministic: dp_{min(id_a, id_b)}_{max(id_a, id_b)}
On each run the script loads any existing CSV and skips pair_ids already present.

Usage (from backend/)
---------------------
    python scripts/generate_eval_candidates.py
    python scripts/generate_eval_candidates.py --max-positives 50 --max-negatives 20
    python scripts/generate_eval_candidates.py --dry-run   # name-match only, no LLM calls

Environment
-----------
    DATABASE_URL   — via core/db/session.py (loads infra/.env)
    GROQ_API_KEY   — via infra/.env (loaded by nodes.py on import)
"""

import argparse
import csv
import logging
import random
import re
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path / env setup — mirrors run_eval.py exactly
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parent.parent   # .../backend/
_ROOT_DIR = _BACKEND_DIR.parent                          # .../mediAid_langraph/

if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(dotenv_path=_ROOT_DIR / "infra" / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Output path
# ---------------------------------------------------------------------------

OUT_DIR = _BACKEND_DIR / "pipeline_outputs"
OUT_DIR.mkdir(exist_ok=True)
OUT_FILE = OUT_DIR / "eval_candidates_raw.csv"

# ---------------------------------------------------------------------------
# CSV column layout
# ---------------------------------------------------------------------------

COLUMNS = [
    "pair_id",
    "drug_a_brand", "drug_a_generic", "drug_a_id",
    "drug_b_brand", "drug_b_generic", "drug_b_id",
    "source_drug", "matched_text",
    "proposed_claim", "proposed_citation", "proposed_final_status",
    # --- reviewer columns (left blank) ---
    "your_decision", "corrected_claim", "corrected_citation", "corrected_status", "notes",
]

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _sync_url(url: str) -> str:
    """Convert postgresql+asyncpg:// → postgresql:// for psycopg2."""
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", url)


def load_drugs_from_db() -> list[dict]:
    """
    Return a list of drug records, each shaped as::

        {
            "id": int,
            "brand_name": str,
            "dosage_form": str | None,
            "generics": [str, ...],          # all generic_names for this drug
            "fda_text": str,                 # concatenated, UNTRUNCATED FDA text
            "fda_by_ingredient": [           # per-ingredient breakdown
                {
                    "generic_name": str,
                    "drug_interactions": str,
                    "warnings": str,
                    "boxed_warning": str,
                }
            ]
        }

    Handles drugs with multiple ingredients (combination drugs) by joining all
    their FDA label fields together under one record.
    """
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        logger.error("psycopg2 not installed. Run: pip install psycopg2-binary")
        sys.exit(1)

    from core.db.session import DATABASE_URL

    conn = psycopg2.connect(_sync_url(DATABASE_URL))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT
            d.id           AS drug_id,
            d.brand_name,
            d.dosage_form,
            di.generic_name,
            fl.drug_interactions,
            fl.warnings,
            fl.boxed_warning
        FROM drugs d
        JOIN drug_ingredients di ON di.drug_id = d.id
        LEFT JOIN fda_labels fl   ON fl.rxcui   = di.rxcui
        ORDER BY d.id, di.id
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()

    # Group by drug_id
    drugs: dict[int, dict] = {}
    for r in rows:
        did = r["drug_id"]
        if did not in drugs:
            drugs[did] = {
                "id": did,
                "brand_name": r["brand_name"],
                "dosage_form": r["dosage_form"],
                "generics": [],
                "fda_by_ingredient": [],
                "fda_text": "",
            }
        ing_name = r["generic_name"] or ""
        if ing_name and ing_name not in drugs[did]["generics"]:
            drugs[did]["generics"].append(ing_name)

        ing_entry = {
            "generic_name": ing_name,
            "drug_interactions": r["drug_interactions"] or "",
            "warnings": r["warnings"] or "",
            "boxed_warning": r["boxed_warning"] or "",
        }
        drugs[did]["fda_by_ingredient"].append(ing_entry)

    # Build concatenated untruncated FDA text per drug (same format as _collect_fda_text
    # in nodes.py but with NO character cap — this is the full reference text)
    for drug in drugs.values():
        parts = []
        for ing in drug["fda_by_ingredient"]:
            name = ing["generic_name"]
            for field in ("drug_interactions", "warnings", "boxed_warning"):
                text = ing[field]
                if text and text.strip():
                    label = field.replace("_", " ").title()
                    parts.append(f"[{name} — {label}]\n{text.strip()}")
        drug["fda_text"] = "\n\n".join(parts)

    result = list(drugs.values())
    logger.info("Loaded %d drugs from the database.", len(result))
    return result


# ---------------------------------------------------------------------------
# Name-matching helpers
# ---------------------------------------------------------------------------

def _build_name_tokens(drug: dict) -> list[str]:
    """
    Return all name tokens for a drug that should be searched in other drugs' FDA text.

    Includes:
    - Brand name: each whitespace-split word stripped of non-alpha chars, lowercased,
      minimum 4 chars (avoids matching short abbreviations like "mg", "tab").
    - Generic names: full lowercased name + first word if multi-word (minimum 5 chars
      for first-word tokens to reduce noise).

    Returns deduplicated list preserving insertion order.
    """
    tokens: list[str] = []

    # Brand name words
    for word in drug["brand_name"].split():
        cleaned = re.sub(r"[^a-zA-Z]", "", word).lower()
        if len(cleaned) >= 4:
            tokens.append(cleaned)

    # Generic names
    for generic in drug["generics"]:
        generic_lower = generic.lower().strip()
        if len(generic_lower) >= 4:
            tokens.append(generic_lower)
        # Also add the first significant word of multi-word generics
        first_word = re.sub(r"[^a-zA-Z]", "", generic_lower.split()[0]) if generic_lower else ""
        if len(first_word) >= 5 and first_word not in tokens:
            tokens.append(first_word)

    return list(dict.fromkeys(tokens))  # deduplicate, preserve order


def _extract_surrounding_sentence(text: str, token: str) -> str:
    """
    Find `token` (case-insensitive, word-boundary) in `text` and return the
    sentence that contains it.  Falls back to a 200-char window if sentence
    boundaries can't be found cleanly.  Result is capped at 400 chars.
    """
    lower_text = text.lower()
    pattern = r"\b" + re.escape(token.lower()) + r"\b"
    m = re.search(pattern, lower_text)
    if not m:
        return ""

    idx = m.start()

    # Walk backward to find sentence start (last .!? followed by whitespace)
    start = max(0, idx - 300)
    prefix = text[start:idx]
    sent_start_offset = max(
        (m2.end() for m2 in re.finditer(r"[.!?]\s+", prefix)),
        default=0,
    )
    abs_start = start + sent_start_offset

    # Walk forward to find sentence end
    suffix = text[idx:]
    sent_end_m = re.search(r"[.!?](?:\s|$)", suffix)
    abs_end = idx + (sent_end_m.end() if sent_end_m else min(200, len(suffix)))

    snippet = text[abs_start:abs_end].strip()
    if len(snippet) > 400:
        snippet = snippet[:400] + "…"
    return snippet


def find_positive_pairs(drugs: list[dict]) -> list[dict]:
    """
    Scan each drug's FDA text for mentions of any other drug's name tokens.

    Returns a list of candidate dicts (one per unordered pair where a mention
    was found in at least one direction).  The `source_drug` field indicates
    which drug's FDA text contained the mention.  If both directions match,
    only one candidate is emitted (the first direction found wins).

    Each result dict has keys used later by the LLM caller and CSV writer:
        pair_id, drug_a_id, drug_a_brand, drug_a_generic,
        drug_b_id, drug_b_brand, drug_b_generic,
        source_drug, matched_text,
        _drug_a_fda, _drug_b_fda   (private — for LLM call, stripped before CSV write)
    """
    name_tokens_by_id: dict[int, list[str]] = {
        d["id"]: _build_name_tokens(d) for d in drugs
    }
    drugs_by_id: dict[int, dict] = {d["id"]: d for d in drugs}

    seen_pair_ids: set[str] = set()
    candidates: list[dict] = []

    for drug_a in drugs:
        a_id = drug_a["id"]
        a_fda_lower = drug_a["fda_text"].lower()
        if not a_fda_lower:
            continue

        for drug_b in drugs:
            b_id = drug_b["id"]
            if a_id == b_id:
                continue

            pair_id = f"dp_{min(a_id, b_id)}_{max(a_id, b_id)}"
            if pair_id in seen_pair_ids:
                continue  # already found from the other direction

            # Check if drug A's FDA text mentions any of drug B's name tokens
            matched_token: Optional[str] = None
            for token in name_tokens_by_id[b_id]:
                pattern = r"\b" + re.escape(token) + r"\b"
                if re.search(pattern, a_fda_lower):
                    matched_token = token
                    break

            if matched_token:
                seen_pair_ids.add(pair_id)
                snippet = _extract_surrounding_sentence(drug_a["fda_text"], matched_token)
                candidates.append({
                    "pair_id": pair_id,
                    "drug_a_id": a_id,
                    "drug_a_brand": drug_a["brand_name"],
                    "drug_a_generic": ", ".join(drug_a["generics"]),
                    "drug_b_id": b_id,
                    "drug_b_brand": drug_b["brand_name"],
                    "drug_b_generic": ", ".join(drug_b["generics"]),
                    "source_drug": drug_a["brand_name"],
                    "matched_text": snippet,
                    # Private fields for LLM — not written to CSV
                    "_drug_a_fda": drug_a["fda_text"],
                    "_drug_b_fda": drug_b["fda_text"],
                })

    logger.info("Name-matching found %d true-positive candidate pairs.", len(candidates))
    return candidates


def find_negative_pairs(
    drugs: list[dict],
    positive_pair_ids: set[str],
    max_negatives: int,
) -> list[dict]:
    """
    Return up to max_negatives drug pairs that have no name-mention overlap
    in either direction.  Sampled with a fixed seed (42) for reproducibility.

    Pairs already in positive_pair_ids are excluded.
    """
    name_tokens_by_id: dict[int, list[str]] = {
        d["id"]: _build_name_tokens(d) for d in drugs
    }
    drugs_by_id: dict[int, dict] = {d["id"]: d for d in drugs}
    all_ids = [d["id"] for d in drugs]

    negatives: list[dict] = []
    checked_pairs: set[str] = set()

    for i, a_id in enumerate(all_ids):
        for b_id in all_ids[i + 1:]:
            pair_id = f"dp_{min(a_id, b_id)}_{max(a_id, b_id)}"
            if pair_id in positive_pair_ids or pair_id in checked_pairs:
                continue
            checked_pairs.add(pair_id)

            drug_a = drugs_by_id[a_id]
            drug_b = drugs_by_id[b_id]
            a_fda_lower = drug_a["fda_text"].lower()
            b_fda_lower = drug_b["fda_text"].lower()

            # Both directions must have no mention for this to be a true negative
            a_mentions_b = any(
                re.search(r"\b" + re.escape(tok) + r"\b", a_fda_lower)
                for tok in name_tokens_by_id[b_id]
            )
            if a_mentions_b:
                continue

            b_mentions_a = any(
                re.search(r"\b" + re.escape(tok) + r"\b", b_fda_lower)
                for tok in name_tokens_by_id[a_id]
            )
            if b_mentions_a:
                continue

            negatives.append({
                "pair_id": pair_id,
                "drug_a_id": a_id,
                "drug_a_brand": drug_a["brand_name"],
                "drug_a_generic": ", ".join(drug_a["generics"]),
                "drug_b_id": b_id,
                "drug_b_brand": drug_b["brand_name"],
                "drug_b_generic": ", ".join(drug_b["generics"]),
                "source_drug": "",
                "matched_text": "",
            })

    logger.info("Found %d true-negative candidate pairs (before sampling).", len(negatives))

    if len(negatives) > max_negatives:
        random.seed(42)  # deterministic sample for reproducibility
        negatives = random.sample(negatives, max_negatives)
        logger.info(
            "Sampled %d true-negative pairs (--max-negatives=%d).",
            len(negatives), max_negatives,
        )

    return negatives


# ---------------------------------------------------------------------------
# LLM call — reuses production chain verbatim
# ---------------------------------------------------------------------------

def draft_llm_candidate(candidate: dict) -> dict:
    """
    Call CHECK_INTERACTION_PROMPT with both drugs' full FDA text to draft
    proposed_claim, proposed_citation, proposed_final_status.

    Imports _get_llm and _build_interaction_chain directly from nodes.py so
    the identical model / temperature / chain setup is reused — no new LLM
    configuration here.

    The candidate dict is mutated in-place and returned.
    """
    from worker.langgraph_pipeline.nodes import _get_llm, _build_interaction_chain

    drug_a_name = candidate["drug_a_brand"]
    drug_b_name = candidate["drug_b_brand"]
    drug_a_fda = candidate.get("_drug_a_fda") or "No FDA label data available."
    drug_b_fda = candidate.get("_drug_b_fda") or "No FDA label data available."

    logger.info("  LLM ← %s ↔ %s", drug_a_name, drug_b_name)

    try:
        llm = _get_llm()
        chain = _build_interaction_chain(llm)
        result = chain.invoke({
            "drug_a_name": drug_a_name,
            "drug_b_name": drug_b_name,
            "drug_a_fda_text": drug_a_fda,
            "drug_b_fda_text": drug_b_fda,
        })

        candidate["proposed_claim"] = result.claim or ""
        candidate["proposed_citation"] = result.citation or ""
        candidate["proposed_final_status"] = (
            "interaction_found" if result.interaction_found else "none_found"
        )
        logger.info(
            "  LLM → interaction_found=%s | claim=%s",
            result.interaction_found,
            (result.claim or "")[:80],
        )
    except Exception as exc:
        logger.error(
            "  LLM call failed for %s ↔ %s: %s", drug_a_name, drug_b_name, exc,
        )
        candidate["proposed_claim"] = f"[LLM ERROR: {exc}]"
        candidate["proposed_citation"] = ""
        candidate["proposed_final_status"] = "error"

    return candidate


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------

def load_existing_pair_ids() -> set[str]:
    """Load pair_ids already present in the output CSV, if it exists."""
    if not OUT_FILE.exists():
        return set()
    with open(OUT_FILE, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        ids = {row["pair_id"] for row in reader if row.get("pair_id")}
    logger.info(
        "Existing CSV has %d pair_ids — will skip these on re-run.", len(ids)
    )
    return ids


def append_rows_to_csv(rows: list[dict]) -> None:
    """Append rows to the output CSV.  Creates the file with headers if new."""
    file_exists = OUT_FILE.exists()
    with open(OUT_FILE, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for row in rows:
            # Ensure all reviewer columns exist as empty strings
            for col in (
                "your_decision", "corrected_claim",
                "corrected_citation", "corrected_status", "notes",
            ):
                row.setdefault(col, "")
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(max_positives: int, max_negatives: int, dry_run: bool) -> None:
    # 1. Load all drugs with FDA text
    drugs = load_drugs_from_db()
    if not drugs:
        logger.error("No drugs found in the database. Exiting.")
        sys.exit(1)

    # 2. Load existing pair_ids for re-run safety
    existing_ids = load_existing_pair_ids()

    # 3. True-positive discovery (deterministic name-matching — no LLM)
    all_positives = find_positive_pairs(drugs)
    new_positives = [c for c in all_positives if c["pair_id"] not in existing_ids]
    logger.info(
        "%d true-positive pairs total, %d not yet in CSV.",
        len(all_positives), len(new_positives),
    )

    # Cap to --max-positives
    if len(new_positives) > max_positives:
        new_positives = new_positives[:max_positives]
        logger.info(
            "Capped to %d true-positive candidates (--max-positives=%d).",
            len(new_positives), max_positives,
        )

    if dry_run:
        logger.info(
            "[DRY RUN] Would process %d positive and up to %d negative candidates.",
            len(new_positives), max_negatives,
        )
        for c in new_positives:
            logger.info(
                "  %s: %s ↔ %s  (source=%s, token found in FDA text)",
                c["pair_id"], c["drug_a_brand"], c["drug_b_brand"], c["source_drug"],
            )
        return

    # 4. LLM calls for true-positive candidates
    drafted_positives: list[dict] = []
    total = len(new_positives)
    for i, candidate in enumerate(new_positives):
        logger.info(
            "[%d/%d] Drafting: %s", i + 1, total, candidate["pair_id"],
        )
        drafted = draft_llm_candidate(candidate)
        drafted_positives.append(drafted)

    # 5. True-negative sampling (no LLM)
    all_positive_ids = {c["pair_id"] for c in all_positives} | existing_ids
    negatives_raw = find_negative_pairs(drugs, all_positive_ids, max_negatives)
    # Further filter: skip any that already exist in the CSV
    new_negatives = [n for n in negatives_raw if n["pair_id"] not in existing_ids]

    for neg in new_negatives:
        neg["proposed_claim"] = ""
        neg["proposed_citation"] = ""
        neg["proposed_final_status"] = "none_found"

    # 6. Write all new rows to CSV
    all_new_rows = drafted_positives + new_negatives
    if all_new_rows:
        append_rows_to_csv(all_new_rows)
        logger.info(
            "Wrote %d new rows to %s.", len(all_new_rows), OUT_FILE.name,
        )
    else:
        logger.info("No new rows to write — all pairs already present in CSV.")

    # 7. Print summary
    tp_found = sum(
        1 for c in drafted_positives
        if c.get("proposed_final_status") == "interaction_found"
    )
    tp_none = sum(
        1 for c in drafted_positives
        if c.get("proposed_final_status") == "none_found"
    )
    tp_err = sum(
        1 for c in drafted_positives
        if c.get("proposed_final_status") == "error"
    )

    print(f"\n{'='*62}")
    print(f"  generate_eval_candidates — Summary")
    print(f"{'='*62}")
    print(f"  Drugs loaded from DB:                         {len(drugs)}")
    print(f"  True-positive candidate pairs found:          {len(all_positives)}")
    print(f"  True-positive candidates drafted (LLM):       {len(drafted_positives)}")
    print(f"    → proposed_final_status = interaction_found: {tp_found}")
    print(f"    → proposed_final_status = none_found:        {tp_none}")
    print(f"    → LLM errors:                                {tp_err}")
    print(f"  True-negative candidates (no LLM):            {len(new_negatives)}")
    print(f"  Total new rows written:                        {len(all_new_rows)}")
    print(f"  Output: {OUT_FILE}")
    print(f"{'='*62}")
    print()
    print("Next steps:")
    print("  1. python scripts/export_fda_reference.py")
    print("     → generates eval_fda_reference.csv + eval_fda_reference.md")
    print("  2. Review eval_candidates_raw.csv")
    print("     → fill your_decision (approve / reject / edit) and corrected_* fields")
    print("  3. python scripts/import_reviewed_eval_candidates.py")
    print("     → pushes approved/edited rows to LangSmith dataset")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Generate LLM-assisted eval candidates for the drug interaction "
            "pipeline.  Outputs eval_candidates_raw.csv."
        )
    )
    parser.add_argument(
        "--max-positives",
        type=int,
        default=50,
        metavar="N",
        help=(
            "Maximum number of true-positive candidate pairs to process with "
            "the LLM.  Pairs are ordered by drug_id so results are stable "
            "across runs.  Default: 50."
        ),
    )
    parser.add_argument(
        "--max-negatives",
        type=int,
        default=20,
        metavar="N",
        help=(
            "Maximum number of true-negative (no-mention) pairs to include. "
            "Sampled with seed=42 for reproducibility.  Default: 20."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run name-matching only, print pairs found, "
            "skip LLM calls and CSV write."
        ),
    )
    args = parser.parse_args()
    main(
        max_positives=args.max_positives,
        max_negatives=args.max_negatives,
        dry_run=args.dry_run,
    )
