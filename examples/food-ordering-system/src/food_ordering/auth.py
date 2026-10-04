"""Passwords and the app's JWTs. The JWT subject is the user's Permit user key."""

import dataclasses
import datetime

import bcrypt
import jwt

ALGORITHM = "HS256"
TOKEN_LIFETIME = datetime.timedelta(minutes=30)
# bcrypt reads at most 72 bytes of a password and refuses longer ones.
_MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> bytes:
    """Return the bcrypt hash of `password`."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt())


def check_password(password: str, hashed: bytes) -> bool:
    """Return whether `password` matches `hashed`."""
    encoded = password.encode()
    if len(encoded) > _MAX_PASSWORD_BYTES:
        return False
    return bcrypt.checkpw(encoded, hashed)


def issue_token(username: str, secret: str, *, now: datetime.datetime | None = None) -> str:
    """Return a signed JWT whose subject is `username`, valid for 30 minutes from `now`."""
    issued = now or datetime.datetime.now(datetime.UTC)
    claims = {"sub": username, "iat": issued, "exp": issued + TOKEN_LIFETIME}
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


@dataclasses.dataclass(frozen=True)
class Claims:
    """What a valid token says: who signed in, and until when the sign-in holds."""

    subject: str
    expires: datetime.datetime


def verify_token(token: str, secret: str) -> Claims | None:
    """Return the claims of a valid token, or None.

    A token is valid when `secret` signed it with HS256, it has not expired, and it carries
    `sub`, `iat` and `exp`.
    """
    try:
        claims = jwt.decode(
            token, secret, algorithms=[ALGORITHM], options={"require": ["sub", "iat", "exp"]}
        )
    except jwt.InvalidTokenError:
        return None
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        return None
    return Claims(subject, datetime.datetime.fromtimestamp(claims["exp"], datetime.UTC))
