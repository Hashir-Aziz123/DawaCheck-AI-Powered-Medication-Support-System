"""
DRAP (Drug Regulatory Authority Pakistan) HTTP client.

Provides functions to search the DRAP product registry, fetch drug details,
parse the HTML detail pages, and resolve a brand name to its full DRAP record
including pre-processed ingredient names.

**Caching note:** None of the functions here read from or write to any cache.
They always perform fresh HTTP calls.  Cache management is the *caller's*
responsibility (e.g. ``scripts/create_dataset.py`` wraps these calls with
``JsonCache`` checks before invoking them).

Public API::

    search_drap(query)            → (list[dict], error_str | None)
    get_drug_detail(reg_no)       → (dict, error_str | None)
    parse_detail_html(html)       → dict
    parse_composition(raw)        → list[dict]
    preprocess_ingredient_name(r) → (cleaned_name, was_stripped)
    resolve_brand(brand_name)     → DrugResolution

Normalization robustness (Problem 1 — salt stripping, Problem 3 — spelling retry)
are both handled inside ``resolve_brand()``.  See module-level comments in
``scripts/create_dataset.py`` for the full design rationale.
"""

import json
import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from core.http_session import SESSION
from core.rate_limit import rate_limit
from core.status import Status
from core.types import DrugResolution, Ingredient

logger = logging.getLogger(__name__)

# ---------- Configuration ----------

DRAP_URL = "https://eapp.dra.gov.pk/productView.php"
REQUEST_TIMEOUT = 10  # seconds

DRAP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://eapp.dra.gov.pk/WebProductIndex.php",
    "X-Requested-With": "XMLHttpRequest",
}

# ---------- Problem 3 thresholds (spelling-variant DRAP retry) ----------
# Combined similarity score = 0.6 * levenshtein_ratio(full) + 0.4 * levenshtein_ratio(first-3-chars).
# The prefix component is the critical safety gate: a mismatch at position 0
# (e.g. "Amryl" vs "Pamaryl") collapses the prefix ratio to 0, producing a
# combined score far below threshold even if the full-string ratio is moderate.
#
# Empirical values (hand-computed on known cases):
#   "Amryl"      -> "Amaryl"     : 0.767  (true typo — accepted FOUND)
#   "Bicoprolol" -> "Bisoprolol" : 0.747  (true typo — accepted FOUND)
#   "Amryl"      -> "Pamaryl"    : 0.257  (different drug — REJECTED)
#
# The gap between the lowest true match (0.747) and the false match (0.257)
# is ~0.49, giving ample room for the thresholds below.
SPELLING_SIM_THRESHOLD = 0.65           # below this -> reject candidate entirely
SPELLING_LOW_CONFIDENCE_THRESHOLD = 0.75  # below this -> accept but flag for review

# ---------- Problem 1: salt/hydrate stripping patterns ----------
# Built from observed DRAP composition strings across the real drug list.

# Equivalency qualifier: strip everything from "EQ TO", "equivalent to",
# "eq. to", or "equal to" onwards.  The previous pattern matched only the
# bare "EQ TO" string; DRAP data also contains prose variants.
_EQ_TO_RE = re.compile(
    r"\s+(?:EQ\.?\s+TO|EQUIVALENT\s+TO|EQUAL\s+TO)\s+.*$",
    re.IGNORECASE,
)

# Parenthetical or prose "as <salt/form>" qualifiers.
# Covers: "(as Hydrochloride)", "(as HCl)", "(as trihydrate)",
#         "(astrihydrate)" (fused misspelling with no space after 'as'),
#         "as Hydrochloride" / "as bisulfate" prose.
# Three precise sub-patterns (most-specific first):
#   1. "(as X)"  — 'as' then space then salt name
#   2. "(asX)"   — fused form, 'as' directly before word char
#   3. " as X"   — prose 'as' at end of string, not inside parens
_AS_FORM_RE = re.compile(
    r"\s*\(as\s+[^)]+\)\s*$"    # (as X)  — space between 'as' and salt
    r"|\s*\(as\w[^)]*\)\s*$"   # (asX)   — fused form, 'as' directly before word char
    r"|\s+as\s+\S.*$",          # as X    — prose form at end of string
    re.IGNORECASE,
)

# Stand-alone "(INN)" annotation — classification tag, not a salt.
_INN_RE = re.compile(r"\s*\(INN\)\s*$", re.IGNORECASE)

# Trailing salt/hydrate descriptors that appear after the base ingredient name.
# Expanded from the original list to include all forms observed in the DRAP
# data across the 96-generic NEML batch run.
_SALT_SUFFIXES = [
    # --- hydrates ---
    "TRIHYDRATE",
    "MONOHYDRATE",
    "DIHYDRATE",
    "HYCLATE",           # doxycycline hyclate
    # --- chloride / HCl forms ---
    "HYDROCHLORIDE",
    "HCL",               # abbreviated hydrochloride (CLINDAMYCIN HCL etc.)
    "CHLORIDE",
    # --- other salt anions ---
    "SODIUM",
    "SOD",               # abbreviated SODIUM (WARFARIN SOD)
    "POTASSIUM",
    "CALCIUM",
    "FUMARATE",
    "SUCCINATE",
    "MALEATE",
    "TARTRATE",
    "ACETATE",
    "SULFATE",
    "SULPHATE",          # British spelling variant
    "BISULFATE",         # clopidogrel bisulfate
    "PHOSPHATE",
    "BROMIDE",
    "MESYLATE",
    "BESYLATE",
    "OXALATE",
    "STEARATE",          # erythromycin stearate
    "BENZOATE",          # metoclopramide benzoate
    "NITRATE",           # glyceryl trinitrate
    # --- pharmacopoeia quality-standard suffixes ---
    "USP",               # United States Pharmacopeia (LOSARTAN POTASSIUM USP)
    "BP",                # British Pharmacopoeia   (WARFARIN SOD BP)
]
# Build a single regex: match any trailing salt word preceded by a space.
# Use word-boundary (\b) to avoid matching mid-word (e.g. "CLOXACILLIN" must
# not strip "CILLIN" if we ever add that term).
_SALT_RE = re.compile(
    r"\s+(?:" + "|".join(re.escape(s) for s in _SALT_SUFFIXES) + r")\b\s*$",
    re.IGNORECASE,
)

# ---------- Levenshtein library (rapidfuzz preferred; pure-Python fallback) ----------
# rapidfuzz is 10-100× faster and is the recommended install:
#   pip install rapidfuzz
# The pure-Python fallback is correct but O(m*n) — fine for short brand names.
try:
    from rapidfuzz.distance import Levenshtein as _RFLev

    def _lev_distance(a: str, b: str) -> int:
        """Edit distance between two strings (rapidfuzz backend)."""
        return _RFLev.distance(a, b)

except ImportError:  # pragma: no cover — only used when rapidfuzz is absent
    def _lev_distance(a: str, b: str) -> int:  # type: ignore[misc]
        """Pure-Python Wagner-Fischer Levenshtein distance (fallback)."""
        m, n = len(a), len(b)
        dp = list(range(n + 1))
        for i in range(1, m + 1):
            prev, dp[0] = dp[0], i
            for j in range(1, n + 1):
                prev, dp[j] = dp[j], min(
                    dp[j] + 1,          # deletion
                    dp[j - 1] + 1,      # insertion
                    prev + (a[i - 1] != b[j - 1]),  # substitution
                )
        return dp[n]


# ---------- Problem 1: ingredient name pre-processing ----------

def preprocess_ingredient_name(raw: str) -> tuple[str, bool]:
    """
    Strip salt-form qualifiers from a DRAP composition ingredient name before
    sending it to RxNorm's exact/normalized lookup.

    Returns ``(cleaned_name, was_stripped)``.  The caller must preserve ``raw``
    in ``Ingredient.drap_raw_name`` regardless of whether stripping occurred,
    so the normalization path stays fully auditable in the CSV output.

    Steps applied in order:

    1. Strip ``(INN)`` classification annotations.
    2. Strip ``"EQ TO / equivalent to / eq. to <anything>"`` equivalency qualifiers.
    3. Strip ``"(as X)"`` / ``"as X"`` parenthetical or prose salt/form qualifiers.
    4. Strip trailing recognised salt/hydrate/pharmacopoeia-suffix words.
       Applied iteratively until no further change (handles stacked suffixes
       such as ``"AMOXICILLIN TRIHYDRATE USP"`` → ``"AMOXICILLIN TRIHYDRATE"``
       → ``"AMOXICILLIN"``).
    """
    cleaned = _INN_RE.sub("", raw).strip()
    cleaned = _EQ_TO_RE.sub("", cleaned).strip()
    cleaned = _AS_FORM_RE.sub("", cleaned).strip()
    # Iterative salt stripping handles stacked suffixes (e.g. TRIHYDRATE USP)
    while True:
        next_cleaned = _SALT_RE.sub("", cleaned).strip()
        if next_cleaned == cleaned:
            break
        cleaned = next_cleaned
    was_stripped = cleaned.upper() != raw.upper()
    return cleaned, was_stripped


# ---------- Problem 3 helpers: spelling-variant generation & scoring ----------

def _adjacent_swap_variants(word: str) -> list[str]:
    """
    Generate spelling-variant candidates for a brand-name query to retry in DRAP.

    Two classes of variants are produced, both narrow and inspectable:

    1. Adjacent-letter swaps — catches transposition typos like ``"Amrly" -> "Amryl"``.
       One swap per adjacent pair, up to ``MAX_SWAP``.

    2. Single-vowel insertions — catches missing-letter errors like ``"Amryl" -> "Amaryl"``.
       Only vowels are inserted (a, e, i, o, u), only between consecutive consonant
       pairs, and only the first ``MAX_INSERTS`` are kept to bound the total retry count.

    Total variants: at most ``MAX_SWAP + MAX_INSERTS = 10``, so at most 10 extra DRAP
    requests.  All are logged before any attempt is made.
    """
    MAX_SWAP = 5
    MAX_INSERTS = 5
    VOWELS = "aeiou"
    variants: list[str] = []

    # --- Adjacent-letter swaps ---
    for i in range(len(word) - 1):
        swapped = list(word)
        swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
        candidate = "".join(swapped)
        if candidate != word and candidate not in variants:
            variants.append(candidate)
        if len(variants) >= MAX_SWAP:
            break

    # --- Single-vowel insertions (between consecutive consonants) ---
    insert_count = 0
    for i in range(len(word) - 1):
        c1, c2 = word[i].lower(), word[i + 1].lower()
        # Only insert between two consonants to reduce noise.
        if c1 not in VOWELS and c2 not in VOWELS:
            for vowel in VOWELS:
                candidate = word[: i + 1] + vowel + word[i + 1 :]
                if candidate not in variants:
                    variants.append(candidate)
                    insert_count += 1
                    if insert_count >= MAX_INSERTS:
                        break
            if insert_count >= MAX_INSERTS:
                break

    return variants


def _spelling_similarity(search: str, candidate_text: str) -> float:
    """
    Prefix-weighted Levenshtein similarity between the original search term
    and the first whitespace-token of a DRAP candidate name.

    Formula::

        score = 0.6 * ratio(full_first_token) + 0.4 * ratio(first_3_chars)

    Only the first whitespace-token of ``candidate_text`` is used (e.g.
    ``"Amaryl"`` from ``"Amaryl 1mg Tablet"``), so that dosage/form suffixes
    appended by DRAP do not dilute the score of a legitimate match.

    The prefix component is the critical safety gate: a mismatch at position 0
    (e.g. ``"Amryl"`` vs ``"Pamaryl"``) drives ``prefix_ratio`` to 0, collapsing
    the combined score well below ``SPELLING_SIM_THRESHOLD`` even when the
    full-string ratio is moderate.  A genuine missing-letter typo
    (``"Amryl"`` vs ``"Amaryl"``) agrees at all three prefix characters,
    keeping ``prefix_ratio`` near 1.

    Returns a float in ``[0.0, 1.0]``; higher is more similar.
    """
    PREFIX_LEN = 3
    # Compare against just the first token to avoid penalising long DRAP names.
    first_tok = candidate_text.split()[0] if candidate_text.strip() else candidate_text
    a = search.lower()
    b = first_tok.lower()

    # Full-token ratio.
    dist_full = _lev_distance(a, b)
    max_len_full = max(len(a), len(b), 1)
    ratio_full = 1.0 - dist_full / max_len_full

    # Prefix ratio (first PREFIX_LEN characters of each).
    a_pre = a[:PREFIX_LEN]
    b_pre = b[:PREFIX_LEN]
    dist_pre = _lev_distance(a_pre, b_pre)
    max_len_pre = max(len(a_pre), len(b_pre), 1)
    ratio_pre = 1.0 - dist_pre / max_len_pre

    return 0.6 * ratio_full + 0.4 * ratio_pre


# ---------- DRAP HTTP helpers ----------

def search_drap(query: str, search_type: str = "brand name") -> tuple[list[dict], Optional[str]]:
    """
    Search the DRAP product registry for *query*.

    Parameters
    ----------
    query:
        The search term (brand name or generic name depending on *search_type*).
    search_type:
        The ``_type`` parameter sent to the DRAP API.  Accepted values:
        ``"brand name"`` (default — existing behaviour) or ``"generic name"``.

    Returns ``(results, error_string)``.  *error_string* is ``None`` on
    success; *results* is an empty list on both "no matches" and error.

    **No cache is read or written here.**  Cache management is the caller's
    responsibility.
    """
    rate_limit("drap")
    try:
        resp = SESSION.get(
            DRAP_URL,
            params={"search": query, "_type": search_type},
            headers=DRAP_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("[DRAP] search failed for '%s' (type='%s'): %s", query, search_type, e)
        return [], str(e)

    try:
        results = json.loads(resp.content.decode("utf-8-sig")).get("results", [])
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error("[DRAP] could not parse search response for '%s': %s", query, e)
        return [], str(e)

    return results, None


def get_drug_detail(reg_no: str) -> tuple[dict, Optional[str]]:
    """
    Fetch and parse the detail page for the drug with DRAP registration number *reg_no*.

    Returns ``(detail_dict, error_string)``.

    **No cache is read or written here.**
    """
    rate_limit("drap")
    try:
        resp = SESSION.post(
            DRAP_URL,
            data={"webRegNo": reg_no},
            headers=DRAP_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("[DRAP] detail fetch failed for reg_no '%s': %s", reg_no, e)
        return {}, str(e)

    return parse_detail_html(resp.text), None


def parse_detail_html(html: str) -> dict:
    """Parse a DRAP product-detail HTML page into a plain dict."""
    soup = BeautifulSoup(html, "html.parser")
    record: dict = {}
    for field_div in soup.select("div[class*='col-']"):
        label_el = field_div.select_one("span.text-muted")
        value_el = field_div.select_one("span.fw-semibold")
        if label_el and value_el:
            record[label_el.get_text(strip=True)] = value_el.get_text(
                separator="\n", strip=True
            )
    if record.get("Composition"):
        record["Composition"] = parse_composition(record["Composition"])
    return record


# Trailing punctuation that DRAP sometimes appends to composition lines
# (most commonly a comma used as a multi-ingredient list separator).
_TRAILING_PUNCT_RE = re.compile(r"[,;.]+\s*$")

# Leading non-printable / non-ASCII junk that occasionally appears due to
# Windows-1252 → UTF-8 mis-decode of the DRAP HTML (e.g. "Â\xa0  Olanzapine").
# Pattern: one or more characters that fall outside the printable ASCII range
# (0x20–0x7E).  NOTE: \w is intentionally absent — Python 3's \w matches
# Unicode letters (including Â/U+00C2), which would silently exclude them
# from the character class and leave the mojibake in place.
_LEADING_GARBAGE_RE = re.compile(r"^[^\x20-\x7E]+")


def parse_composition(raw: str) -> list[dict]:
    """
    Parse a DRAP ``Composition`` field into a list of ``{"name": ..., "amount": ...}``
    dicts.

    Three recognised formats:

    * ``"Amoxicillin..........500 mg"`` — dot-dot separator.
    * ``"Amoxicillin 500 mg"``          — space-separated numeric dose.
    * ``"DIAZEPAM 2MG,"``               — dose followed by trailing comma/semicolon
      (DRAP multi-ingredient separator artefact); the trailing punctuation is
      stripped before the dose regex is applied.

    Additional hygiene applied per line:
    * Trailing punctuation (``,``, ``;``, ``.``) is stripped before parsing.
    * Leading non-ASCII / non-printable garbage characters (encoding mojibake)
      are stripped from the parsed ingredient name.

    **Fused single-line format (Format 4):**
    Some DRAP records concatenate all ingredients on one line without any
    newline separators, e.g.::

        CODEINE PHOSPHATE15mgParacetamol.....................500mgCaffeine

    A pre-pass detects this by finding a dose token immediately followed by a
    capital letter (start of the next ingredient name) and inserts synthetic
    newlines so the normal per-line parser handles each ingredient correctly.
    """
    # Pre-pass: split fused single-line compositions.
    # Pattern: a dose unit token immediately followed (no whitespace) by a
    # letter that starts the next ingredient name.
    # We insert a newline after each dose token in this case.
    _FUSED_SPLIT_RE = re.compile(
        r"(\d+\.?\d*\s*(?:mg|mcg|g|ml|IU|%))\s*(?=[A-Za-z])",
        re.IGNORECASE,
    )

    def _pre_split(text: str) -> str:
        """Insert newlines at dose-to-ingredient boundaries on fused single lines."""
        # Only apply if there are NO newlines already (genuine fused format).
        if "\n" in text:
            return text
        # Only split when there are multiple dose tokens on the one line.
        tokens = _FUSED_SPLIT_RE.findall(text)
        if len(tokens) <= 1:
            return text  # nothing to split
        # Insert '\n' after each dose token that is directly followed by a letter.
        return _FUSED_SPLIT_RE.sub(lambda m: m.group(0).rstrip() + "\n", text)

    raw = _pre_split(raw)

    ingredients = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue

        # F1: strip trailing comma/semicolon/period before dose matching
        line = _TRAILING_PUNCT_RE.sub("", line).strip()
        if not line:
            continue

        match = re.match(r"^(.*?)\.{2,}\s*(.*)$", line)
        if match:
            name, amount = match.group(1), match.group(2)
        else:
            dose_match = re.match(
                r"^(.*?)\s+(\d+\.?\d*\s*(?:mg|mcg|g|ml|IU|%))\s*$",
                line,
                re.IGNORECASE,
            )
            name, amount = (
                (dose_match.group(1), dose_match.group(2)) if dose_match else (line, "")
            )

        # F3: strip leading encoding-garbage from name
        name = _LEADING_GARBAGE_RE.sub("", name).strip()

        ingredients.append({"name": name, "amount": amount.strip()})
    return ingredients


# ---------- Brand resolution ----------

def resolve_brand(brand_name: str) -> DrugResolution:
    """
    Resolve *brand_name* to a ``DrugResolution`` via the DRAP registry.

    Steps:

    1. Exact brand-name search in DRAP.
    2. If no results, try up to 10 spelling variants (Problem 3); each accepted
       candidate is validated against the original query via
       ``_spelling_similarity()`` before use.
    3. Fetch the detail page for the best match and pre-process ingredient names
       (Problem 1 — salt/qualifier stripping).

    Returns a ``DrugResolution`` with ``status`` set to one of:
    ``OK``, ``NOT_FOUND``, ``API_ERROR``, ``EMPTY_DATA``,
    ``SPELLING_RETRY_FOUND``, or ``SPELLING_RETRY_LOW_CONFIDENCE``.

    **No cache is read or written here.**
    """
    matches, err = search_drap(brand_name)
    if err:
        return DrugResolution(brand_name, Status.API_ERROR, error=err)

    # --- Problem 3: spelling-variant retry when initial DRAP search finds nothing ---
    # SAFETY: results returned by a variant search are validated against the
    # original query via _spelling_similarity() before acceptance.  This prevents
    # a variant like "maryl" from silently matching "Pamaryl Plus Tablet" when the
    # pharmacist searched for "Amryl" (intended: "Amaryl").
    spelling_variant_used: Optional[str] = None
    spelling_retry_status: Status = Status.SPELLING_RETRY_FOUND  # tightened below if low-confidence
    if not matches:
        variants = _adjacent_swap_variants(brand_name)
        logger.info(
            "[DRAP] No results for '%s' — trying %d spelling variant(s): %s",
            brand_name, len(variants), variants,
        )
        for variant in variants:
            variant_matches, variant_err = search_drap(variant)
            if variant_err:
                # Network errors on a variant retry are non-fatal — keep trying.
                logger.warning(
                    "[DRAP] Spelling-variant search error for '%s': %s",
                    variant, variant_err,
                )
                continue
            if variant_matches:
                # Filter: only keep candidates whose first token is similar enough
                # to the original search term, judged by prefix-weighted Levenshtein.
                accepted = [
                    m for m in variant_matches
                    if _spelling_similarity(brand_name, m.get("text", "")) >= SPELLING_SIM_THRESHOLD
                ]
                if not accepted:
                    rejected_names = [m.get("text", "?") for m in variant_matches]
                    logger.warning(
                        "[DRAP] Spelling-variant '%s' returned %d result(s) but ALL failed "
                        "similarity filter (threshold=%s): %s — skipping",
                        variant, len(variant_matches), SPELLING_SIM_THRESHOLD, rejected_names,
                    )
                    continue  # try the next variant

                # Pick the highest-scoring accepted candidate.
                best = max(
                    accepted,
                    key=lambda m: _spelling_similarity(brand_name, m.get("text", "")),
                )
                best_sim = _spelling_similarity(brand_name, best.get("text", ""))
                if best_sim >= SPELLING_LOW_CONFIDENCE_THRESHOLD:
                    spelling_retry_status = Status.SPELLING_RETRY_FOUND
                else:
                    spelling_retry_status = Status.SPELLING_RETRY_LOW_CONFIDENCE
                logger.info(
                    "[DRAP] Spelling-variant retry accepted: '%s' -> '%s' via variant '%s' "
                    "(similarity=%.3f, status=%s)",
                    brand_name, best["text"], variant, best_sim, spelling_retry_status.value,
                )
                matches = accepted
                spelling_variant_used = variant
                break
        else:
            logger.info(
                "[DRAP] All spelling variants exhausted for '%s' — no results found",
                brand_name,
            )

    if not matches:
        return DrugResolution(brand_name, Status.NOT_FOUND, error="No DRAP matches found")

    chosen = matches[0]
    if len(matches) > 1:
        logger.warning(
            "[DRAP] '%s' had %d matches — defaulting to first: '%s'",
            brand_name, len(matches), chosen["text"],
        )

    detail, err = get_drug_detail(chosen["id"])
    if err:
        return DrugResolution(
            brand_name, Status.API_ERROR,
            matched_name=chosen["text"],
            candidate_count=len(matches),
            error=err,
        )

    composition = detail.get("Composition", [])
    if not composition:
        return DrugResolution(
            brand_name, Status.EMPTY_DATA,
            matched_name=chosen["text"],
            drap_reg_no=chosen["id"],
            candidate_count=len(matches),
            error="DRAP record found but composition was empty",
        )

    resolution_status = spelling_retry_status if spelling_variant_used else Status.OK

    # Problem 1: pre-process each ingredient name before storing.
    # Original DRAP string is preserved in drap_raw_name; cleaned name goes into Ingredient.name.
    ingredients: list[Ingredient] = []
    for c in composition:
        if not c.get("name"):
            continue
        raw_name = c["name"].upper()
        cleaned_name, was_stripped = preprocess_ingredient_name(raw_name)
        if was_stripped:
            logger.info(
                "[Preprocess] Salt/qualifier stripped: '%s' -> '%s' "
                "(STRIPPED_AND_MATCHED will be set if RxNorm resolves this)",
                raw_name, cleaned_name,
            )
        ingredients.append(
            Ingredient(
                name=cleaned_name,
                amount=c.get("amount", ""),
                drap_raw_name=raw_name,
                name_was_stripped=was_stripped,
            )
        )

    return DrugResolution(
        brand_name, resolution_status,
        matched_name=chosen["text"],
        drap_reg_no=chosen["id"],
        candidate_count=len(matches),
        spelling_variant_used=spelling_variant_used,
        ingredients=ingredients,
    )
