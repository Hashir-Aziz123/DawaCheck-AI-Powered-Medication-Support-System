import uuid
from datetime import datetime
from sqlalchemy import Text, ForeignKey, CheckConstraint, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Drug(Base):
    __tablename__ = "drugs"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_name: Mapped[str] = mapped_column(Text, nullable=False)
    search_name: Mapped[str | None] = mapped_column(Text, index=True)
    drap_reg_no: Mapped[str | None] = mapped_column(Text)          # internal only — never returned by the API
    dosage_form: Mapped[str | None] = mapped_column(Text)
    company_name: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    ingredients: Mapped[list["DrugIngredient"]] = relationship(
        back_populates="drug", cascade="all, delete-orphan"
    )


class DrugIngredient(Base):
    __tablename__ = "drug_ingredients"
    __table_args__ = (
        UniqueConstraint("drug_id", "generic_name", "dose", name="uq_drug_ingredient"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    drug_id: Mapped[int] = mapped_column(ForeignKey("drugs.id", ondelete="CASCADE"), nullable=False)
    generic_name: Mapped[str] = mapped_column(Text, nullable=False)   # as parsed from DRAP, e.g. "Guaifenesin"
    dose: Mapped[str | None] = mapped_column(Text)                     # "100 mg"
    rxcui: Mapped[str | None] = mapped_column(Text, index=True)          # stable RxNorm concept ID — canonical join key
    rxnorm_name: Mapped[str | None] = mapped_column(Text)                 # human-readable, stored alongside rxcui

    drug: Mapped["Drug"] = relationship(back_populates="ingredients")


class FdaLabel(Base):
    __tablename__ = "fda_labels"

    id: Mapped[int] = mapped_column(primary_key=True)
    rxcui: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    rxnorm_name: Mapped[str] = mapped_column(Text, nullable=False)
    drug_interactions: Mapped[str | None] = mapped_column(Text)
    warnings: Mapped[str | None] = mapped_column(Text)
    boxed_warning: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[dict | None] = mapped_column(JSONB)          # full openFDA payload, future-proofing
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())


class InteractionJob(Base):
    __tablename__ = "interaction_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'processing', 'done', 'failed')",
            name="valid_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    drug_a_id: Mapped[int | None] = mapped_column(ForeignKey("drugs.id"))
    drug_b_id: Mapped[int | None] = mapped_column(ForeignKey("drugs.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column()