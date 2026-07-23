from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError


password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("The password must contain at least 12 characters")
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
