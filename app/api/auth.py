from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import create_access_token, require_user, verify_password
from app.db.session import get_db
from app.models.auth import StoreAccess, User
from app.schemas.auth import LoginRequest, MeResponse, StoreGrantOut, TokenResponse

router = APIRouter()


@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")

    token, expires_in = create_access_token(user.id)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/auth/me", response_model=MeResponse)
def get_me(user: User = Depends(require_user), db: Session = Depends(get_db)) -> MeResponse:
    grants = db.execute(select(StoreAccess).where(StoreAccess.user_id == user.id)).scalars().all()
    return MeResponse(
        user_id=user.id,
        email=user.email,
        store_access=[StoreGrantOut(store_id=grant.store_id, role=grant.role) for grant in grants],
    )
