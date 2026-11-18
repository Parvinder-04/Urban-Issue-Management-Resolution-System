"""
Auth Router — Secure native bcrypt password hashing, JWT authentication,
and Role-Based Access Control (RBAC) dependencies.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy.orm import Session

from app import models, schemas
from app.core.config import get_settings
from app.database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()
security = HTTPBearer(auto_error=False)

# Bcrypt standard maximum password length in bytes
BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    """Hash password using native bcrypt with safe length enforcement.

    Does not silently truncate passwords. Raises ValueError if length exceeds 72 bytes.
    """
    pw_bytes = password.encode("utf-8")
    if len(pw_bytes) > BCRYPT_MAX_BYTES:
        raise ValueError(f"Password cannot exceed {BCRYPT_MAX_BYTES} bytes")
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pw_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password against bcrypt hash safely without throwing exceptions on long or invalid inputs."""
    if not plain_password or not hashed_password:
        return False
    pw_bytes = plain_password.encode("utf-8")
    if len(pw_bytes) > BCRYPT_MAX_BYTES:
        # Passwords exceeding 72 bytes cannot match a valid bcrypt hash; fail safely
        return False
    try:
        hash_bytes = hashed_password.encode("utf-8")
        return bcrypt.checkpw(pw_bytes, hash_bytes)
    except (ValueError, TypeError):
        return False


def create_access_token(
    user_or_id: Union[models.User, str],
    role: Optional[str] = None,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Create a signed JWT access token containing subject, role, and expiration."""
    if isinstance(user_or_id, models.User):
        user_id = str(user_or_id.id)
        email = user_or_id.email
        user_role = (
            user_or_id.role.value
            if isinstance(user_or_id.role, models.UserRole)
            else str(user_or_id.role).lower()
        )
    else:
        user_id = str(user_or_id)
        email = None
        user_role = role or "citizen"

    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    payload = {
        "sub": user_id,
        "exp": expire,
        "iat": now,
    }
    if email:
        payload["email"] = email
    if user_role:
        payload["role"] = user_role

    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> models.User:
    """FastAPI dependency to extract and validate the authenticated user from the Bearer token."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
        user_id: Optional[str] = payload.get("sub")
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials: missing subject",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_role(allowed_roles: Union[list[Union[models.UserRole, str]], models.UserRole, str]):
    """FastAPI dependency factory enforcing Role-Based Access Control (RBAC)."""
    if not isinstance(allowed_roles, list):
        roles_list = [allowed_roles]
    else:
        roles_list = allowed_roles

    allowed_set = set()
    for r in roles_list:
        if isinstance(r, models.UserRole):
            allowed_set.add(r.value.lower())
        else:
            allowed_set.add(str(r).lower())

    def role_checker(current_user: models.User = Depends(get_current_user)) -> models.User:
        user_role_str = (
            current_user.role.value.lower()
            if isinstance(current_user.role, models.UserRole)
            else str(current_user.role).lower()
        )
        if user_role_str not in allowed_set:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Operation not permitted. Required role: {', '.join(sorted(allowed_set))}",
            )
        return current_user

    return role_checker


@router.post("/register", response_model=schemas.UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: schemas.UserCreate, db: Session = Depends(get_db)):
    """Register a new citizen user account."""
    existing = db.query(models.User).filter(models.User.email == payload.email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    try:
        hashed_password = hash_password(payload.password)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    user = models.User(
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        password_hash=hashed_password,
        role=models.UserRole.CITIZEN,
        preferred_language=payload.preferred_language,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=schemas.TokenResponse)
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    """Authenticate with email and password via JSON request body, returning a JWT bearer token."""
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(user)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": schemas.UserOut.model_validate(user),
    }


@router.get("/me", response_model=schemas.UserOut)
def get_profile(current_user: models.User = Depends(get_current_user)):
    """Retrieve profile of the currently authenticated user."""
    return current_user
