import uuid
from datetime import datetime

from sqlalchemy import Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow
from app.models.enums import CorrelationStatus


class PosTransaction(Base):
    """(store_id, order_id) is the idempotency key, enforced at the database
    level -- previously this was only checked with a SELECT-then-insert in
    PosIngestionService, which is a race under concurrent import of the same
    file/order."""

    __tablename__ = "pos_transaction"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id: Mapped[str] = mapped_column(String, index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    items: Mapped[list["PosTransactionItem"]] = relationship(back_populates="transaction", cascade="all, delete-orphan")
    correlations: Mapped[list["TransactionCorrelation"]] = relationship(back_populates="transaction", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("store_id", "order_id", name="uq_pos_transaction_store_order_id"),
    )

class PosTransactionItem(Base):
    __tablename__ = "pos_transaction_item"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    transaction_id: Mapped[str] = mapped_column(ForeignKey("pos_transaction.id"), index=True)
    product_id: Mapped[str] = mapped_column(String)
    brand_name: Mapped[str | None] = mapped_column(String)
    amount: Mapped[float] = mapped_column(Float)

    transaction: Mapped["PosTransaction"] = relationship(back_populates="items")

class TransactionCorrelation(Base):
    __tablename__ = "transaction_correlation"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    transaction_id: Mapped[str | None] = mapped_column(ForeignKey("pos_transaction.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("visit_session.id"), index=True)
    status: Mapped[CorrelationStatus] = mapped_column(default=CorrelationStatus.MATCHED, index=True)
    confidence_score: Mapped[float] = mapped_column(Float)
    correlation_method: Mapped[str] = mapped_column(String)
    explanation: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    transaction: Mapped["PosTransaction"] = relationship(back_populates="correlations")
    session: Mapped["VisitSession"] = relationship(back_populates="correlations")
