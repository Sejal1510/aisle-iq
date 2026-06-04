from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal, init_db
from app.models.enums import EventType, SessionStatus
from app.models.event import Event
from app.models.tracking import TrackedEntity, VisitSession


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "demo_pos_st1001_st1002.csv"
DEFAULT_POS_SAMPLE_PATH = PROJECT_ROOT / "data" / "POS - sample transactionsb1e826f (1).csv"
DEFAULT_STORE_TARGETS = {"ST1001": 8, "ST1002": 5}
CSV_FIELDS = [
    "order_id",
    "order_date",
    "order_time",
    "store_id",
    "product_id",
    "brand_name",
    "total_amount",
]


@dataclass(frozen=True)
class ProductPattern:
    product_id: str
    brand_name: str | None
    amount: float


@dataclass(frozen=True)
class DemoTransaction:
    order_id: str
    store_id: str
    session_id: str
    timestamp: datetime
    items: list[ProductPattern]


@dataclass
class DemoPosGenerationResult:
    output_path: Path
    transactions: list[DemoTransaction] = field(default_factory=list)
    checkout_events_added: int = 0
    checkout_events_reused: int = 0

    @property
    def row_count(self) -> int:
        return sum(len(transaction.items) for transaction in self.transactions)

    @property
    def transaction_count(self) -> int:
        return len(self.transactions)

    @property
    def transaction_counts_by_store(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for transaction in self.transactions:
            counts[transaction.store_id] = counts.get(transaction.store_id, 0) + 1
        return counts


def generate_demo_pos(
    db: Session,
    *,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    product_source_path: Path = DEFAULT_POS_SAMPLE_PATH,
    store_targets: dict[str, int] | None = None,
    seed_checkout_events: bool = True,
) -> DemoPosGenerationResult:
    """Generate a transparent demo POS fixture aligned to real CCTV sessions."""
    store_targets = store_targets or DEFAULT_STORE_TARGETS
    product_patterns = load_product_patterns(product_source_path)
    transactions = build_demo_transactions(db, product_patterns, store_targets)

    result = DemoPosGenerationResult(output_path=output_path, transactions=transactions)
    if seed_checkout_events:
        result.checkout_events_added, result.checkout_events_reused = seed_demo_checkout_events(
            db,
            transactions,
        )

    write_demo_pos_csv(output_path, transactions)
    return result


def load_product_patterns(pos_sample_path: Path) -> list[ProductPattern]:
    patterns: list[ProductPattern] = []
    seen_product_ids: set[str] = set()

    with pos_sample_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        for row in csv.DictReader(csv_file):
            product_id = str(row.get("product_id", "")).strip()
            if not product_id or product_id in seen_product_ids:
                continue

            try:
                amount = float(str(row.get("total_amount", "0")).strip())
            except ValueError:
                continue

            brand_name = str(row.get("brand_name", "")).strip() or None
            patterns.append(ProductPattern(product_id=product_id, brand_name=brand_name, amount=amount))
            seen_product_ids.add(product_id)

    if not patterns:
        raise ValueError(f"No usable product patterns found in {pos_sample_path}")
    return patterns


def build_demo_transactions(
    db: Session,
    product_patterns: list[ProductPattern],
    store_targets: dict[str, int],
) -> list[DemoTransaction]:
    transactions: list[DemoTransaction] = []

    for store_id, target_count in store_targets.items():
        sessions = _eligible_sessions(db, store_id, target_count)
        for index, session in enumerate(sessions, start=1):
            checkout_ts = _checkout_timestamp(session, index)
            transactions.append(
                DemoTransaction(
                    order_id=f"DEMO-{store_id}-{index:03d}",
                    store_id=store_id,
                    session_id=session.id,
                    timestamp=checkout_ts + timedelta(seconds=20),
                    items=_transaction_items(product_patterns, store_id, index),
                )
            )

    return transactions


def seed_demo_checkout_events(
    db: Session,
    transactions: Iterable[DemoTransaction],
) -> tuple[int, int]:
    added = 0
    reused = 0

    for transaction in transactions:
        queue_event_id = f"{transaction.order_id}-CHECKOUT"
        existing_event = db.execute(
            select(Event.id).where(Event.queue_event_id == queue_event_id)
        ).first()
        if existing_event is not None:
            reused += 1
            continue

        session = db.get(VisitSession, transaction.session_id)
        if session is None:
            continue

        checkout_ts = transaction.timestamp - timedelta(seconds=20)
        queue_join_ts = checkout_ts - timedelta(seconds=120)
        queue_served_ts = checkout_ts - timedelta(seconds=5)
        db.add(
            Event(
                session_id=session.id,
                tracked_entity_id=session.tracked_entity_id,
                store_id=session.store_id,
                camera_id=None,
                zone_id=None,
                event_type=EventType.QUEUE_COMPLETED,
                timestamp=checkout_ts,
                hotspot_x=None,
                hotspot_y=None,
                is_face_hidden=None,
                queue_event_id=queue_event_id,
                queue_join_ts=queue_join_ts,
                queue_served_ts=queue_served_ts,
                queue_exit_ts=checkout_ts,
                wait_seconds=int((queue_served_ts - queue_join_ts).total_seconds()),
                queue_position_at_join=(added % 3) + 1,
                abandoned=False,
            )
        )
        added += 1

    db.flush()
    return added, reused


def write_demo_pos_csv(output_path: Path, transactions: Iterable[DemoTransaction]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for transaction in transactions:
            for item in transaction.items:
                writer.writerow(
                    {
                        "order_id": transaction.order_id,
                        "order_date": transaction.timestamp.strftime("%d-%m-%Y"),
                        "order_time": transaction.timestamp.strftime("%H:%M:%S"),
                        "store_id": transaction.store_id,
                        "product_id": item.product_id,
                        "brand_name": item.brand_name or "",
                        "total_amount": f"{item.amount:.2f}",
                    }
                )


def _eligible_sessions(db: Session, store_id: str, target_count: int) -> list[VisitSession]:
    sessions = list(
        db.scalars(
            select(VisitSession)
            .join(TrackedEntity)
            .where(VisitSession.store_id == store_id)
            .where(VisitSession.session_status.in_([SessionStatus.IN_PROGRESS, SessionStatus.COMPLETED]))
            .where(TrackedEntity.is_staff.is_not(True))
            .order_by(VisitSession.entry_time, VisitSession.id)
        )
    )

    clean_sessions = [session for session in sessions if not _has_queue_abandonment(db, session.id)]
    return clean_sessions[:target_count]


def _has_queue_abandonment(db: Session, session_id: str) -> bool:
    return (
        db.execute(
            select(Event.id).where(
                Event.session_id == session_id,
                Event.event_type == EventType.QUEUE_ABANDONED,
            )
        ).first()
        is not None
    )


def _checkout_timestamp(session: VisitSession, sequence: int) -> datetime:
    anchor = session.exit_time or session.entry_time + timedelta(minutes=4)
    return anchor + timedelta(minutes=sequence * 12)


def _transaction_items(
    product_patterns: list[ProductPattern],
    store_id: str,
    sequence: int,
) -> list[ProductPattern]:
    item_count = 2 if sequence % 3 == 0 else 1
    store_multiplier = 1.08 if store_id == "ST1001" else 0.94
    items: list[ProductPattern] = []

    for offset in range(item_count):
        pattern = product_patterns[(sequence + offset - 1) % len(product_patterns)]
        amount = round(max(1.0, pattern.amount * store_multiplier + (sequence % 5) * 13.0), 2)
        items.append(
            ProductPattern(
                product_id=pattern.product_id,
                brand_name=pattern.brand_name,
                amount=amount,
            )
        )

    return items


def _result_payload(result: DemoPosGenerationResult) -> dict[str, object]:
    return {
        "output_path": str(result.output_path),
        "rows": result.row_count,
        "transactions": result.transaction_count,
        "transaction_counts_by_store": result.transaction_counts_by_store,
        "checkout_events_added": result.checkout_events_added,
        "checkout_events_reused": result.checkout_events_reused,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate demo POS rows for ST1001/ST1002.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--product-source", type=Path, default=DEFAULT_POS_SAMPLE_PATH)
    parser.add_argument(
        "--skip-checkout-events",
        action="store_true",
        help="Only write the POS CSV; do not seed generated demo checkout completion events.",
    )
    args = parser.parse_args()

    init_db()
    with SessionLocal() as db:
        result = generate_demo_pos(
            db,
            output_path=args.output,
            product_source_path=args.product_source,
            seed_checkout_events=not args.skip_checkout_events,
        )
        db.commit()

    print(json.dumps(_result_payload(result), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
