from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.enums import CorrelationStatus, EventType, SessionStatus
from app.models.event import Event
from app.models.pos import PosTransaction, TransactionCorrelation
from app.models.tracking import TrackedEntity, VisitSession


@dataclass(frozen=True)
class CorrelationCandidate:
    session: VisitSession
    transaction: PosTransaction
    confidence_score: float
    method: str
    explanation: dict[str, object]


class CorrelationService:
    def __init__(
        self,
        db: Session,
        *,
        queue_window: timedelta = timedelta(minutes=10),
        exit_window: timedelta = timedelta(minutes=20),
        session_window: timedelta = timedelta(minutes=45),
        minimum_confidence: float = 0.55,
        ambiguity_delta: float = 0.10,
    ):
        self.db = db
        self.queue_window = queue_window
        self.exit_window = exit_window
        self.session_window = session_window
        self.minimum_confidence = minimum_confidence
        self.ambiguity_delta = ambiguity_delta

    def correlate_all(self, *, replace_existing: bool = False) -> list[TransactionCorrelation]:
        if replace_existing:
            self.db.execute(delete(TransactionCorrelation))

        sessions = self._eligible_sessions()
        transactions = self._transactions()
        correlations: list[TransactionCorrelation] = []

        candidates_by_transaction = {
            transaction.id: self._rank_candidates(transaction, sessions)
            for transaction in transactions
        }
        matched_session_ids: set[str] = set()
        matched_transaction_ids: set[str] = set()

        for transaction in transactions:
            candidates = candidates_by_transaction[transaction.id]
            persisted = self._persist_transaction_result(transaction, candidates)
            correlations.extend(persisted)

            if candidates and candidates[0].confidence_score >= self.minimum_confidence:
                top_score = candidates[0].confidence_score
                top_candidates = [
                    candidate
                    for candidate in candidates
                    if top_score - candidate.confidence_score <= self.ambiguity_delta
                ]
                matched_transaction_ids.add(transaction.id)
                matched_session_ids.update(candidate.session.id for candidate in top_candidates)

        for session in sessions:
            if session.id not in matched_session_ids:
                correlations.append(self._persist_unmatched_visitor(session))

        self.db.flush()
        return correlations

    def score_candidate(self, session: VisitSession, transaction: PosTransaction) -> CorrelationCandidate:
        if session.store_id != transaction.store_id:
            return CorrelationCandidate(
                session=session,
                transaction=transaction,
                confidence_score=0.0,
                method="store_mismatch",
                explanation={"store_match": False},
            )

        queue_event = self._latest_queue_completion(session.id)
        queue_score = 0.0
        queue_seconds = None
        if queue_event is not None:
            queue_seconds = self._seconds_between(queue_event.timestamp, transaction.timestamp)
            queue_score = self._temporal_score(queue_seconds, self.queue_window)

        exit_score = 0.0
        exit_seconds = None
        if session.exit_time is not None:
            exit_seconds = self._seconds_between(session.exit_time, transaction.timestamp)
            exit_score = self._temporal_score(exit_seconds, self.exit_window)

        session_seconds = self._seconds_from_session(session, transaction.timestamp)
        session_score = self._temporal_score(session_seconds, self.session_window)
        has_queue_abandonment = self._has_queue_abandonment(session.id)

        score = 0.20
        score += queue_score * 0.45
        score += exit_score * 0.25
        score += session_score * 0.10
        if has_queue_abandonment:
            score -= 0.20

        score = max(0.0, min(score, 1.0))
        method = "queue_exit_time" if queue_event is not None else "session_exit_time"

        return CorrelationCandidate(
            session=session,
            transaction=transaction,
            confidence_score=round(score, 4),
            method=method,
            explanation={
                "store_match": True,
                "queue_seconds_delta": queue_seconds,
                "exit_seconds_delta": exit_seconds,
                "session_seconds_delta": session_seconds,
                "queue_score": round(queue_score, 4),
                "exit_score": round(exit_score, 4),
                "session_score": round(session_score, 4),
                "has_queue_abandonment": has_queue_abandonment,
                "minimum_confidence": self.minimum_confidence,
            },
        )

    def _persist_transaction_result(
        self,
        transaction: PosTransaction,
        candidates: list[CorrelationCandidate],
    ) -> list[TransactionCorrelation]:
        if not candidates or candidates[0].confidence_score < self.minimum_confidence:
            return [
                self._add_correlation(
                    status=CorrelationStatus.UNMATCHED,
                    transaction_id=transaction.id,
                    session_id=None,
                    confidence_score=0.0,
                    method="no_eligible_visitor",
                    explanation={
                        "reason": "no visitor candidate met the confidence threshold",
                        "minimum_confidence": self.minimum_confidence,
                    },
                )
            ]

        top_score = candidates[0].confidence_score
        top_candidates = [
            candidate
            for candidate in candidates
            if top_score - candidate.confidence_score <= self.ambiguity_delta
        ]
        status = CorrelationStatus.AMBIGUOUS if len(top_candidates) > 1 else CorrelationStatus.MATCHED

        return [
            self._add_correlation(
                status=status,
                transaction_id=candidate.transaction.id,
                session_id=candidate.session.id,
                confidence_score=candidate.confidence_score,
                method=candidate.method,
                explanation={
                    **candidate.explanation,
                    "candidate_rank": rank,
                    "candidate_count": len(top_candidates),
                },
            )
            for rank, candidate in enumerate(top_candidates, start=1)
        ]

    def _persist_unmatched_visitor(self, session: VisitSession) -> TransactionCorrelation:
        return self._add_correlation(
            status=CorrelationStatus.UNMATCHED,
            transaction_id=None,
            session_id=session.id,
            confidence_score=0.0,
            method="no_eligible_transaction",
            explanation={
                "reason": "no POS transaction met the confidence threshold",
                "store_id": session.store_id,
            },
        )

    def _add_correlation(
        self,
        *,
        status: CorrelationStatus,
        transaction_id: str | None,
        session_id: str | None,
        confidence_score: float,
        method: str,
        explanation: dict[str, object],
    ) -> TransactionCorrelation:
        correlation = TransactionCorrelation(
            transaction_id=transaction_id,
            session_id=session_id,
            status=status,
            confidence_score=confidence_score,
            correlation_method=method,
            explanation=json.dumps(explanation, sort_keys=True),
        )
        self.db.add(correlation)
        return correlation

    def _rank_candidates(
        self,
        transaction: PosTransaction,
        sessions: Iterable[VisitSession],
    ) -> list[CorrelationCandidate]:
        candidates = [
            self.score_candidate(session, transaction)
            for session in sessions
            if session.store_id == transaction.store_id
        ]
        candidates = [
            candidate
            for candidate in candidates
            if candidate.confidence_score > 0
        ]
        return sorted(candidates, key=lambda candidate: candidate.confidence_score, reverse=True)

    def _eligible_sessions(self) -> list[VisitSession]:
        return list(
            self.db.scalars(
                select(VisitSession)
                .join(TrackedEntity)
                .where(VisitSession.session_status.in_([SessionStatus.IN_PROGRESS, SessionStatus.COMPLETED]))
                .where(TrackedEntity.is_staff.is_not(True))
                .order_by(VisitSession.entry_time)
            )
        )

    def _transactions(self) -> list[PosTransaction]:
        return list(self.db.scalars(select(PosTransaction).order_by(PosTransaction.timestamp)))

    def _latest_queue_completion(self, session_id: str) -> Event | None:
        return self.db.scalars(
            select(Event)
            .where(
                Event.session_id == session_id,
                Event.event_type == EventType.QUEUE_COMPLETED,
                Event.abandoned.is_(False),
            )
            .order_by(Event.timestamp.desc())
        ).first()

    def _has_queue_abandonment(self, session_id: str) -> bool:
        return (
            self.db.execute(
                select(Event.id).where(
                    Event.session_id == session_id,
                    Event.event_type == EventType.QUEUE_ABANDONED,
                )
            ).first()
            is not None
        )

    def _seconds_from_session(self, session: VisitSession, timestamp: datetime) -> int:
        session_end = session.exit_time or session.entry_time
        if session.entry_time <= timestamp <= session_end:
            return 0
        return min(
            self._seconds_between(session.entry_time, timestamp),
            self._seconds_between(session_end, timestamp),
        )

    @staticmethod
    def _seconds_between(left: datetime, right: datetime) -> int:
        return abs(int((right - left).total_seconds()))

    @staticmethod
    def _temporal_score(seconds: int | None, window: timedelta) -> float:
        if seconds is None:
            return 0.0

        window_seconds = int(window.total_seconds())
        if seconds > window_seconds:
            return 0.0

        return 1.0 - (seconds / window_seconds)
