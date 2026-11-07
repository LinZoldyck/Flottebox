import re
import secrets
from datetime import datetime

_PLATE_RE = re.compile(r"\b([A-Z]{2})[\s-]?(\d{3})[\s-]?([A-Z]{2})\b")
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sans 0/O/1/I


def now_local() -> datetime:
    """Heure locale (fuseau de l'application), sans tzinfo. Toutes les dates sont stockées ainsi."""
    from app.config import get_settings

    return datetime.now(get_settings().tz).replace(tzinfo=None, microsecond=0)


def to_local_naive(value: datetime) -> datetime:
    from app.config import get_settings

    if value.tzinfo is None:
        return value
    return value.astimezone(get_settings().tz).replace(tzinfo=None)


def normalize_plate(value: str | None) -> str:
    """'ab123cd', 'AB 123 CD' -> 'AB-123-CD'. Renvoie la saisie en majuscules si format non SIV."""
    if not value:
        return ""
    up = value.upper().strip()
    m = _PLATE_RE.search(up)
    return "-".join(m.groups()) if m else re.sub(r"\s+", "", up)


def find_plate(text: str) -> str:
    m = _PLATE_RE.search(text.upper())
    return "-".join(m.groups()) if m else ""


def generate_access_code(length: int = 8) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def normalize_phone(value: str) -> str:
    """Format E.164 pour la France : 06 12 34 56 78 -> +33612345678."""
    digits = re.sub(r"[^\d+]", "", value or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        digits = "+33" + digits[1:]
    return digits


def fuel_label(percent: int | None) -> str:
    if percent is None:
        return "-"
    return f"{percent}%"


def euros(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f} €".replace(",", " ").replace(".", ",")
