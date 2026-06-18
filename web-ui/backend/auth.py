"""Password hashing/verification for the Web UI login (stdlib only, no extra deps).

Format of a stored hash:  pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>
Only this hash (never the plaintext password) is stored in SSM / env.

CLI helper (used to seed local .env or SSM):
    python auth.py 'my-password'      -> prints the PBKDF2 hash string
"""
import base64
import hashlib
import hmac
import os
import sys

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 200_000


def hash_password(password, iterations=_ITERATIONS, salt=None):
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "%s$%d$%s$%s" % (
        _ALGO, iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password, stored):
    """Constant-time verify. Returns False on any malformed/empty input."""
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != _ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: python auth.py '<password>'\n")
        sys.exit(2)
    print(hash_password(sys.argv[1]))
