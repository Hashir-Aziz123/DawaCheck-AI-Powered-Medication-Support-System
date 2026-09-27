import sys
import re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from core.db.session import DATABASE_URL
from core.db.models import Drug, FdaLabel, DrugClass

def sync_url(url: str) -> str:
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", url)

def trace_extract(full_text: str, class_names: list[str], side: str):
    print(f"\n{'='*60}\nTracing extraction for: {side}\n{'='*60}")
    print(f"Provided classes: {class_names}")
    
    _GENERIC_TERMS = {
        "inhibitors", "antagonists", "agonists", "agents", "other", "various",
        "class", "combination", "combinations", "derivatives", "preparations",
        "activity", "action", "decreased", "increased", "local", "with",
    }
    stop_words = {"and", "with", "for", "in", "of", "to", "the"}
    
    search_terms: set[str] = set()
    for cls in class_names:
        words = cls.lower().split()
        if len(words) > 1:
            acronym = "".join(w[0] for w in words if w.strip(",.;():") not in stop_words)
            if len(acronym) >= 3:
                search_terms.add(acronym)
        for word in words:
            cleaned = word.strip(",.;():")
            if len(cleaned) > 4 and cleaned not in _GENERIC_TERMS:
                search_terms.add(cleaned)
                if cleaned.endswith("in"):
                    search_terms.add(cleaned[:-2])
                elif cleaned.endswith("ic"):
                    search_terms.add(cleaned[:-2])
                    
    print(f"Generated search terms: {search_terms}")
    if not search_terms:
        print("No search terms generated.")
        return
        
    pattern = r"\b(?:{})\w*\b".format("|".join(map(re.escape, search_terms)))
    print(f"Generated Regex: {pattern}")
    search_regex = re.compile(pattern, re.IGNORECASE)
    
    sentences = re.split(r"(?<=[.!?])\s+", full_text)
    
    seen = set()
    relevant = []
    total_chars = 0
    max_chars = 1000
    
    print("\n--- MATCHING SENTENCES ---")
    for sent in sentences:
        sent = sent.strip()
        if not sent or sent in seen:
            continue
        if search_regex.search(sent):
            seen.add(sent)
            relevant.append(sent)
            total_chars += len(sent) + 1
            print(f"\n[MATCH {len(relevant)} | {len(sent)} chars]:\n{sent}")
            if total_chars >= max_chars:
                print(f"\n--- HIT MAX CHARS LIMIT ({total_chars} >= {max_chars}) ---")
                break
                
    if not relevant:
        print("\nNO SENTENCES MATCHED THE REGEX.")
        
    # Diagnostic: Did we miss "serotonin syndrome"?
    missed = []
    for sent in sentences:
        if "seroton" in sent.lower() and sent.strip() not in seen:
            missed.append(sent.strip())
            
    if missed:
        print("\n*** DIAGNOSTIC WARNING ***")
        print(f"Found {len(missed)} sentence(s) containing 'seroton' that the regex MISSED or SKIPPED due to limit:")
        for i, m in enumerate(missed[:3]):
            print(f"\n[MISSED {i+1}]:\n{m}")
        if len(missed) > 3:
            print(f"... and {len(missed) - 3} more.")

def get_drug_data(session: Session, name: str):
    drug = session.execute(select(Drug).where(Drug.brand_name.ilike(f"%{name}%"))).scalars().first()
    if not drug:
        return None, [], ""
        
    rxcuis = [ing.rxcui for ing in drug.ingredients if ing.rxcui]
    if not rxcuis:
        return drug, [], ""
        
    classes = []
    for rxcui in rxcuis:
        dc = session.execute(select(DrugClass).where(DrugClass.rxcui == rxcui)).scalar_one_or_none()
        if dc and dc.class_names:
            classes.extend(dc.class_names)
            
    text_blocks = []
    for rxcui in rxcuis:
        label = session.execute(select(FdaLabel).where(FdaLabel.rxcui == rxcui)).scalar_one_or_none()
        if label:
            for field in ("drug_interactions", "warnings", "boxed_warning"):
                val = getattr(label, field, None)
                if val:
                    text_blocks.append(val.strip())
                    
    return drug, list(set(classes)), "\n\n".join(text_blocks)

def main():
    engine = create_engine(sync_url(DATABASE_URL))
    with Session(engine) as session:
        a_drug, a_classes, a_text = get_drug_data(session, "Zoloft Tablet 50mg")
        b_drug, b_classes, b_text = get_drug_data(session, "Vexnil SR Tablet 100mg")
        
        if not a_drug or not b_drug:
            print("Could not find one or both drugs in DB.")
            return
            
        print(f"Drug A: {a_drug.brand_name} | Length of FDA text: {len(a_text)}")
        print(f"Drug B: {b_drug.brand_name} | Length of FDA text: {len(b_text)}")
        
        trace_extract(b_text, a_classes, side="Vexnil (Tramadol) FDA label vs Zoloft (Sertraline) classes")
        trace_extract(a_text, b_classes, side="Zoloft (Sertraline) FDA label vs Vexnil (Tramadol) classes")

if __name__ == "__main__":
    main()
