from __future__ import annotations

import os
from pathlib import Path
import shutil

from PIL import Image
import pytesseract


def resolve_tesseract_command(configured: str = "") -> Path | None:
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_dir():
            executable = "tesseract.exe" if os.name == "nt" else "tesseract"
            candidate = candidate / executable
        if candidate.is_file():
            return candidate.resolve()
        discovered = shutil.which(configured)
        return Path(discovered).resolve() if discovered else candidate

    discovered = shutil.which("tesseract")
    return Path(discovered).resolve() if discovered else None


def image_to_text(image_path: Path, lang: str = "chi_sim+eng", tesseract_cmd: str = "") -> str:
    if tesseract_cmd:
        resolved = resolve_tesseract_command(tesseract_cmd)
        if resolved is not None:
            pytesseract.pytesseract.tesseract_cmd = str(resolved)

    with Image.open(image_path) as img:
        text = pytesseract.image_to_string(img, lang=lang)
    return text.strip()
