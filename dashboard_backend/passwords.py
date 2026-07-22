from __future__ import annotations

import secrets


PASSWORD_UPPER = "ABCDEFGHJKMNPQRSTUVWXYZ"
PASSWORD_LOWER = "abcdefghjkmnpqrstuvwxyz"
PASSWORD_DIGITS = "23456789"
PASSWORD_SYMBOLS = "!@#$%"
PASSWORD_ALPHABET = PASSWORD_UPPER + PASSWORD_LOWER + PASSWORD_DIGITS + PASSWORD_SYMBOLS
# Native PVE set-user-password schema limit. Keeping it in one place avoids
# accepting a value in the dashboard that the live API will inevitably reject.
PROXMOX_PASSWORD_MAX_LENGTH = 1024


def generate_password(length: int = 16) -> str:
    """Generate a readable password without commonly confused characters."""
    if length < 4:
        raise ValueError("Password length must be at least 4")
    characters = [
        secrets.choice(PASSWORD_UPPER),
        secrets.choice(PASSWORD_LOWER),
        secrets.choice(PASSWORD_DIGITS),
        secrets.choice(PASSWORD_SYMBOLS),
    ]
    characters.extend(secrets.choice(PASSWORD_ALPHABET) for _ in range(length - len(characters)))
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)
