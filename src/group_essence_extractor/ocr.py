from __future__ import annotations

import os
from pathlib import Path

from PIL import Image
import pytesseract


def image_to_text(image_path: Path, lang: str = "chi_sim+eng", tesseract_cmd: str = "") -> str:
    if tesseract_cmd:
        tesseract_path = Path(tesseract_cmd)
        if tesseract_path.is_dir():
            executable = "tesseract.exe" if os.name == "nt" else "tesseract"
            tesseract_path = tesseract_path / executable
        pytesseract.pytesseract.tesseract_cmd = str(tesseract_path)

    with Image.open(image_path) as img:
        text = pytesseract.image_to_string(img, lang=lang)
    return text.strip()
