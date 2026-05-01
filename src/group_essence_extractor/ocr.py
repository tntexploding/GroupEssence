from __future__ import annotations

from pathlib import Path

from PIL import Image
import pytesseract


def image_to_text(image_path: Path, lang: str = "chi_sim+eng", tesseract_cmd: str = "") -> str:
    if tesseract_cmd:
        tesseract_path = Path(tesseract_cmd)
        if tesseract_path.is_dir():
            tesseract_path = tesseract_path / "tesseract.exe"
        pytesseract.pytesseract.tesseract_cmd = str(tesseract_path)

    img = Image.open(image_path)
    text = pytesseract.image_to_string(img, lang=lang)
    return text.strip()
