import logging
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert

from core.db.models import Drug, DrugIngredient, FdaLabel
from core.resolution import build_drug_record
from core.generic_resolution import build_generic_record, generate_search_name
from core.status import Status
from core.types import DrugResolution
from app.schemas.resolve import DrugResolutionResult, IngredientResponse, ResolvedDrugInfo

logger = logging.getLogger(__name__)


def _drug_to_candidate(drug: Drug) -> ResolvedDrugInfo:
    """Convert a Drug ORM object to a minimal, consumer-safe ResolvedDrugInfo."""
    display_name = _build_display_name(drug.brand_name, drug.dosage_form)
    return ResolvedDrugInfo(display_name=display_name, dosage_form=drug.dosage_form, drug_id=drug.id)


def _build_display_name(brand_name: str, dosage_form: str | None) -> str:
    """Append dosage_form to brand_name only when it isn't already present.

    Prevents duplicates like "Paracetamol Syrup syrup" when the DB brand_name
    already contains the dosage form string.
    """
    if dosage_form and dosage_form.lower() not in brand_name.lower():
        return f"{brand_name} {dosage_form}".strip()
    return brand_name


def _build_db_response(drug: Drug, labels_by_rxcui: dict, matched_as: str) -> DrugResolutionResult:
    ingredients_resp = []
    for ing in drug.ingredients:
        label = labels_by_rxcui.get(ing.rxcui) if ing.rxcui else None
        ingredients_resp.append(IngredientResponse(
            generic_name=ing.generic_name,
            dose=ing.dose,
            rxcui=ing.rxcui,
            drug_interactions=label.drug_interactions if label else None,
            warnings=label.warnings if label else None,
            boxed_warning=label.boxed_warning if label else None,
        ))
        
    return DrugResolutionResult(
        status="found",
        source="database",
        matched_as=matched_as,
        brand_name=drug.brand_name,
        dosage_form=drug.dosage_form,
        ingredients=ingredients_resp,
        drug_id=drug.id,
    )



def _build_live_response(record: DrugResolution, query: str, matched_as: str, drug_id: int | None = None) -> DrugResolutionResult:
    ingredients_resp = []
    for ing in record.ingredients:
        ingredients_resp.append(IngredientResponse(
            generic_name=ing.name,
            dose=ing.amount,
            rxcui=ing.rxcui,
            drug_interactions=ing.drug_interactions,
            warnings=ing.warnings,
            boxed_warning=ing.boxed_warning,
        ))

    return DrugResolutionResult(
        status="found",
        source="live",
        matched_as=matched_as,
        brand_name=record.matched_name or query,
        dosage_form=None,
        ingredients=ingredients_resp,
        drug_id=drug_id,
    )


def _persist_live_record(record: DrugResolution, query: str, db_session: Session) -> int | None:
    """Persist a live-resolved drug to the database and return its integer id.
    Returns None if persistence fails (caller should still return the live response).
    """
    try:
        drug_id = None
        if record.drap_reg_no:
            stmt = select(Drug.id).where(Drug.drap_reg_no == record.drap_reg_no)
            existing_drug_id = db_session.execute(stmt).scalar()
            if existing_drug_id:
                drug_id = existing_drug_id

        if not drug_id:
            brand_name = record.matched_name or query
            search_name = generate_search_name(brand_name)
            drug = Drug(
                brand_name=brand_name,
                search_name=search_name,
                drap_reg_no=record.drap_reg_no,
                dosage_form=None,
                company_name=None
            )
            db_session.add(drug)
            db_session.flush()
            drug_id = drug.id

        for ing in record.ingredients:
            stmt = insert(DrugIngredient).values(
                drug_id=drug_id,
                generic_name=ing.name,
                dose=ing.amount,
                rxcui=ing.rxcui,
                rxnorm_name=ing.rxnorm_name
            ).on_conflict_do_nothing(constraint="uq_drug_ingredient")
            db_session.execute(stmt)

            if ing.rxcui:
                fda_stmt = insert(FdaLabel).values(
                    rxcui=ing.rxcui,
                    rxnorm_name=ing.rxnorm_name or "",
                    drug_interactions=ing.drug_interactions,
                    warnings=ing.warnings,
                    boxed_warning=ing.boxed_warning,
                    raw_response=ing.raw_fda_response
                ).on_conflict_do_nothing(index_elements=["rxcui"])
                db_session.execute(fda_stmt)

        db_session.commit()
        logger.info("Successfully persisted live resolution for '%s' to database (drug_id=%s).", query, drug_id)
        return drug_id
    except Exception as e:
        logger.exception("Failed to persist live resolution for '%s': %s", query, e)
        db_session.rollback()
        return None


def resolve_drug(query: str, db_session: Session) -> DrugResolutionResult:
    """
    Resolve a drug using the sequence: DB Brand -> DB Generic -> Live Brand -> Live Generic.
    """
    logger.info("--- Resolution started for query: '%s' ---", query)
    
    # 1. Database Brand Search
    logger.info("Step 1: DB Brand Search")
    stmt = select(Drug).where(Drug.brand_name.ilike(query))
    brand_drugs = db_session.execute(stmt).scalars().all()

    if len(brand_drugs) > 1:
        logger.warning("Ambiguous DB brand match for '%s' (found %d).", query, len(brand_drugs))
        return DrugResolutionResult(
            status="ambiguous", source="database", matched_as="brand", brand_name=query,
            candidates=[_drug_to_candidate(d) for d in brand_drugs],
        )
    
    if len(brand_drugs) == 1:
        logger.info("DB Brand hit for '%s'.", query)
        drug = brand_drugs[0]
        
        rxcui_list = [ing.rxcui for ing in drug.ingredients if ing.rxcui]
        labels_by_rxcui = {}
        if rxcui_list:
            stmt = select(FdaLabel).where(FdaLabel.rxcui.in_(rxcui_list))
            labels = db_session.execute(stmt).scalars().all()
            labels_by_rxcui = {label.rxcui: label for label in labels}

        return _build_db_response(drug, labels_by_rxcui, matched_as="brand")

    # 2. Database Generic Search
    logger.info("Step 2: DB Generic Search")
    stmt = select(Drug).join(DrugIngredient).where(DrugIngredient.generic_name.ilike(query))
    generic_drugs = db_session.execute(stmt).scalars().unique().all()

    if len(generic_drugs) > 1:
        logger.warning("Ambiguous DB generic match for '%s' (found %d distinct drugs).", query, len(generic_drugs))
        return DrugResolutionResult(
            status="ambiguous", source="database", matched_as="generic", brand_name=query,
            candidates=[_drug_to_candidate(d) for d in generic_drugs],
        )
    
    if len(generic_drugs) == 1:
        logger.info("DB Generic hit for '%s'.", query)
        drug = generic_drugs[0]
        
        rxcui_list = [ing.rxcui for ing in drug.ingredients if ing.rxcui]
        labels_by_rxcui = {}
        if rxcui_list:
            stmt = select(FdaLabel).where(FdaLabel.rxcui.in_(rxcui_list))
            labels = db_session.execute(stmt).scalars().all()
            labels_by_rxcui = {label.rxcui: label for label in labels}

        return _build_db_response(drug, labels_by_rxcui, matched_as="generic")

    # 3. Database Brand Fuzzy Search
    logger.info("Step 3: DB Brand Fuzzy Search")
    # Threshold 0.40: requires meaningful structural similarity, not just shared substrings.
    # 0.20 was too permissive — e.g. "Mefenamic Acid" matched "Folic Acid Tablet" (0.30),
    # blocking the live DRAP fallback and returning a completely wrong drug.
    stmt = select(Drug, func.similarity(Drug.search_name, query).label("sim"))\
        .where(func.similarity(Drug.search_name, query) > 0.40)\
        .order_by(func.similarity(Drug.search_name, query).desc())
    fuzzy_brand_results = db_session.execute(stmt).all()
    
    if fuzzy_brand_results:
        highest_score = fuzzy_brand_results[0].sim
        top_matches = [r for r in fuzzy_brand_results if r.sim == highest_score]
        
        if len(top_matches) > 1:
            logger.warning("Ambiguous DB fuzzy brand match for '%s' (found %d with top score).", query, len(top_matches))
            return DrugResolutionResult(
                status="ambiguous", source="database", matched_as="brand", brand_name=query,
                candidates=[_drug_to_candidate(r.Drug) for r in top_matches],
            )
        else:
            logger.info("DB Fuzzy Brand hit for '%s' -> '%s' (score: %.2f).", query, top_matches[0].Drug.brand_name, highest_score)
            drug = top_matches[0].Drug
            rxcui_list = [ing.rxcui for ing in drug.ingredients if ing.rxcui]
            labels_by_rxcui = {}
            if rxcui_list:
                stmt = select(FdaLabel).where(FdaLabel.rxcui.in_(rxcui_list))
                labels = db_session.execute(stmt).scalars().all()
                labels_by_rxcui = {label.rxcui: label for label in labels}

            return _build_db_response(drug, labels_by_rxcui, matched_as="brand")

    # 4. Database Generic Fuzzy Search
    logger.info("Step 4: DB Generic Fuzzy Search")
    stmt = select(Drug, func.similarity(DrugIngredient.generic_name, query).label("sim"))\
        .join(DrugIngredient)\
        .where(func.similarity(DrugIngredient.generic_name, query) > 0.40)\
        .order_by(func.similarity(DrugIngredient.generic_name, query).desc())
    fuzzy_generic_results = db_session.execute(stmt).all()

    if fuzzy_generic_results:
        highest_score = fuzzy_generic_results[0].sim
        top_drugs = {}
        for r in fuzzy_generic_results:
            if r.sim == highest_score:
                top_drugs[r.Drug.id] = r.Drug
        
        if len(top_drugs) > 1:
            logger.warning("Ambiguous DB fuzzy generic match for '%s' (found %d distinct drugs with top score).", query, len(top_drugs))
            return DrugResolutionResult(
                status="ambiguous", source="database", matched_as="generic", brand_name=query,
                candidates=[_drug_to_candidate(d) for d in top_drugs.values()],
            )
        else:
            drug = list(top_drugs.values())[0]
            logger.info("DB Fuzzy Generic hit for '%s' -> (score: %.2f).", query, highest_score)
            rxcui_list = [ing.rxcui for ing in drug.ingredients if ing.rxcui]
            labels_by_rxcui = {}
            if rxcui_list:
                stmt = select(FdaLabel).where(FdaLabel.rxcui.in_(rxcui_list))
                labels = db_session.execute(stmt).scalars().all()
                labels_by_rxcui = {label.rxcui: label for label in labels}

            return _build_db_response(drug, labels_by_rxcui, matched_as="generic")

    # 5. Live Brand Fallback
    logger.info("Step 5: Live Brand Search")
    brand_record = build_drug_record(query)
    if brand_record.status in (Status.OK, Status.SPELLING_RETRY_FOUND, Status.SPELLING_RETRY_LOW_CONFIDENCE):
        live_drug_id = _persist_live_record(brand_record, query, db_session)
        return _build_live_response(brand_record, query, matched_as="brand", drug_id=live_drug_id)

    # 6. Live Generic Fallback
    logger.info("Step 6: Live Generic Search")
    generic_record = build_generic_record(query)
    if generic_record.status in (Status.OK, Status.SPELLING_RETRY_FOUND, Status.SPELLING_RETRY_LOW_CONFIDENCE):
        live_drug_id = _persist_live_record(generic_record, query, db_session)
        return _build_live_response(generic_record, query, matched_as="generic", drug_id=live_drug_id)

    # Failed all paths
    logger.info("All resolution paths failed for '%s'.", query)
    
    status_mapped = "error" if generic_record.status == Status.API_ERROR else "not_found"
    return DrugResolutionResult(
        status=status_mapped,
        source="live",
        matched_as=None,
        brand_name=query
    )
