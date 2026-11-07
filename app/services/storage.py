"""Stockage des fichiers (photos redimensionnées, PDF, logos) dans data/uploads."""
from __future__ import annotations

import io
import secrets
from pathlib import Path

from PIL import Image, ImageOps

from app.config import get_settings

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


class UploadError(Exception):
    pass


def _target(subdir: str, suffix: str) -> tuple[Path, str]:
    root = get_settings().uploads_dir
    folder = root / subdir
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(8)}{suffix}"
    return folder / name, f"{subdir}/{name}"


def save_image(data: bytes, subdir: str, max_side: int = 1600) -> str:
    """Corrige l'orientation EXIF, redimensionne et enregistre en JPEG. Renvoie le chemin relatif."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError("Fichier trop volumineux (15 Mo max).")
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise UploadError("Image illisible (formats acceptés : JPEG, PNG, WEBP).") from exc
    img.thumbnail((max_side, max_side))
    path, rel = _target(subdir, ".jpg")
    img.save(path, "JPEG", quality=82, optimize=True)
    return rel


def save_file(data: bytes, subdir: str, suffix: str) -> str:
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError("Fichier trop volumineux (15 Mo max).")
    path, rel = _target(subdir, suffix)
    path.write_bytes(data)
    return rel


def absolute(rel: str) -> Path:
    root = get_settings().uploads_dir.resolve()
    path = (root / rel).resolve()
    if root not in path.parents:
        raise UploadError("Chemin invalide")
    return path
