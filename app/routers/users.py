"""User administration: access rights for individual internal users."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.models import User
from app.schemas import UserIn, UserOut, UserUpdate, UserWithKey
from app.security import AdminUser, CurrentUser, DbSession, generate_api_key

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/me", response_model=UserOut, summary="Who am I?")
def me(user: CurrentUser) -> User:
    return user


@router.get("", response_model=list[UserOut])
def list_users(db: DbSession, admin: AdminUser) -> list[User]:
    return list(db.scalars(select(User).order_by(User.email)))


@router.post(
    "",
    response_model=UserWithKey,
    status_code=201,
    summary="Create a user; the API key is returned once, at creation",
)
def create_user(payload: UserIn, db: DbSession, admin: AdminUser) -> User:
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status_code=409, detail="A user with that email already exists")
    user = User(
        email=str(payload.email),
        full_name=payload.full_name,
        role=payload.role,
        api_key=payload.api_key or generate_api_key(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdate, db: DbSession, admin: AdminUser) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id and payload.role and payload.role != admin.role:
        raise HTTPException(status_code=400, detail="Administrators cannot demote themselves")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return user


@router.post("/{user_id}/rotate-key", response_model=UserWithKey)
def rotate_key(user_id: int, db: DbSession, admin: AdminUser) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.api_key = generate_api_key()
    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=204, summary="Deactivate a user")
def deactivate_user(user_id: int, db: DbSession, admin: AdminUser) -> None:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Administrators cannot deactivate themselves")
    user.is_active = False
    db.commit()
