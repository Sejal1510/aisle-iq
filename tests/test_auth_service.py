# P4.4: unit coverage for password hashing, JWT issuing/validation, and the
# require_user / require_store_role dependencies, called directly the same
# way tests/test_analytics_api.py calls route functions directly.
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    create_user,
    decode_access_token,
    grant_store_access,
    hash_password,
    require_store_role,
    require_user,
    verify_password,
)
from app.models import Base
from app.models.auth import StoreAccess
from app.models.enums import Role
from app.models.store import Store


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_hash_password_round_trip() -> None:
    hashed = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password", hashed)


def test_hash_password_salts_each_call_differently() -> None:
    assert hash_password("same-password") != hash_password("same-password")


def test_create_access_token_round_trip() -> None:
    token, expires_in = create_access_token("user-123")

    assert expires_in == get_settings().jwt_expire_minutes * 60
    assert decode_access_token(token) == "user-123"


def test_decode_access_token_rejects_expired_token() -> None:
    settings = get_settings()
    expired_payload = {"sub": "user-123", "exp": datetime.now(UTC) - timedelta(seconds=1)}
    expired_token = jwt.encode(expired_payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(expired_token)


def test_decode_access_token_rejects_bad_signature() -> None:
    token, _ = create_access_token("user-123")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    with pytest.raises(jwt.PyJWTError):
        decode_access_token(tampered)


def test_create_user_persists_hashed_password_not_plaintext(db_session: Session) -> None:
    user = create_user(db_session, email="a@example.com", raw_password="hunter2")
    db_session.commit()

    assert user.password_hash != "hunter2"
    assert verify_password("hunter2", user.password_hash)


def test_grant_store_access_creates_then_updates_role(db_session: Session) -> None:
    db_session.add(Store(id="ST1", name=None))
    user = create_user(db_session, email="a@example.com", raw_password="pw")
    db_session.commit()

    grant_store_access(db_session, user.id, "ST1", Role.ANALYST)
    db_session.commit()
    assert db_session.query(StoreAccess).one().role == Role.ANALYST

    # Re-granting updates the existing row (idempotent on user_id+store_id)
    # rather than violating the unique constraint.
    grant_store_access(db_session, user.id, "ST1", Role.ADMIN)
    db_session.commit()
    assert db_session.query(StoreAccess).count() == 1
    assert db_session.query(StoreAccess).one().role == Role.ADMIN


def test_require_user_rejects_missing_header(db_session: Session) -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_user(authorization=None, db=db_session)
    assert exc_info.value.status_code == 401


def test_require_user_rejects_non_bearer_header(db_session: Session) -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_user(authorization="Basic dXNlcjpwYXNz", db=db_session)
    assert exc_info.value.status_code == 401


def test_require_user_rejects_invalid_token(db_session: Session) -> None:
    with pytest.raises(HTTPException) as exc_info:
        require_user(authorization="Bearer not-a-real-token", db=db_session)
    assert exc_info.value.status_code == 401


def test_require_user_accepts_valid_token(db_session: Session) -> None:
    user = create_user(db_session, email="a@example.com", raw_password="pw")
    db_session.commit()
    token, _ = create_access_token(user.id)

    resolved = require_user(authorization=f"Bearer {token}", db=db_session)

    assert resolved.id == user.id


def test_require_user_rejects_inactive_user(db_session: Session) -> None:
    user = create_user(db_session, email="a@example.com", raw_password="pw")
    user.is_active = False
    db_session.commit()
    token, _ = create_access_token(user.id)

    with pytest.raises(HTTPException) as exc_info:
        require_user(authorization=f"Bearer {token}", db=db_session)
    assert exc_info.value.status_code == 401


def test_require_store_role_allows_matching_grant(db_session: Session) -> None:
    db_session.add(Store(id="ST1", name=None))
    user = create_user(db_session, email="analyst@example.com", raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, user.id, "ST1", Role.ANALYST)
    db_session.commit()

    dependency = require_store_role(Role.ANALYST)
    access = dependency(store_id="ST1", user=user, db=db_session)

    assert access.role == Role.ANALYST


def test_require_store_role_rejects_wrong_store(db_session: Session) -> None:
    db_session.add(Store(id="ST1", name=None))
    db_session.add(Store(id="ST2", name=None))
    user = create_user(db_session, email="analyst@example.com", raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, user.id, "ST1", Role.ANALYST)
    db_session.commit()

    dependency = require_store_role(Role.ANALYST)
    with pytest.raises(HTTPException) as exc_info:
        dependency(store_id="ST2", user=user, db=db_session)
    assert exc_info.value.status_code == 403


def test_require_store_role_enforces_minimum_role(db_session: Session) -> None:
    db_session.add(Store(id="ST1", name=None))
    user = create_user(db_session, email="analyst@example.com", raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, user.id, "ST1", Role.ANALYST)
    db_session.commit()

    admin_only = require_store_role(Role.ADMIN)
    with pytest.raises(HTTPException) as exc_info:
        admin_only(store_id="ST1", user=user, db=db_session)
    assert exc_info.value.status_code == 403


def test_require_store_role_admin_satisfies_lower_minimums(db_session: Session) -> None:
    db_session.add(Store(id="ST1", name=None))
    user = create_user(db_session, email="admin@example.com", raw_password="pw")
    db_session.commit()
    grant_store_access(db_session, user.id, "ST1", Role.ADMIN)
    db_session.commit()

    analyst_gate = require_store_role(Role.ANALYST)
    access = analyst_gate(store_id="ST1", user=user, db=db_session)

    assert access.role == Role.ADMIN
