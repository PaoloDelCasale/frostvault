from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError


password_hasher = PasswordHasher()

# A fixed, valid Argon2 hash keeps ineligible login attempts on the same
# password-verification path as eligible users.  The login handler must still
# require an eligible user separately: this hash is never an authentication
# credential and must not make the dummy path successful.
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$eK6z13o+tvmXpdNzTRIFBA$"
    "pn6XgrlAuMRGPdrPZ+5M9EgbidM8fLjjaK22wZUuPTo"
)


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("The password must contain at least 12 characters")
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
