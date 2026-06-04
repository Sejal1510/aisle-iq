from typing import List, Optional
from sqlalchemy import String, Float, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from sqlalchemy.sql import func
import uuid

from app.db.base import Base
from app.models.enums import CorrelationStatus

class PosTransaction(Base):
    __tablename__ = "pos_transaction"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id: Mapped[str] = mapped_column(String, index=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("store.id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    items: Mapped[List["PosTransactionItem"]] = relationship(back_populates="transaction", cascade="all, delete-orphan")
    correlations: Mapped[List["TransactionCorrelation"]] = relationship(back_populates="transaction", cascade="all, delete-orphan")

class PosTransactionItem(Base):
    __tablename__ = "pos_transaction_item"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    transaction_id: Mapped[str] = mapped_column(ForeignKey("pos_transaction.id"), index=True)
    product_id: Mapped[str] = mapped_column(String)
    brand_name: Mapped[Optional[str]] = mapped_column(String)
    amount: Mapped[float] = mapped_column(Float)

    transaction: Mapped["PosTransaction"] = relationship(back_populates="items")

class TransactionCorrelation(Base):
    __tablename__ = "transaction_correlation"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    transaction_id: Mapped[Optional[str]] = mapped_column(ForeignKey("pos_transaction.id"), index=True)
    session_id: Mapped[Optional[str]] = mapped_column(ForeignKey("visit_session.id"), index=True)
    status: Mapped[CorrelationStatus] = mapped_column(default=CorrelationStatus.MATCHED, index=True)
    confidence_score: Mapped[float] = mapped_column(Float)
    correlation_method: Mapped[str] = mapped_column(String)
    explanation: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=func.now())

    transaction: Mapped["PosTransaction"] = relationship(back_populates="correlations")
    session: Mapped["VisitSession"] = relationship(back_populates="correlations")
