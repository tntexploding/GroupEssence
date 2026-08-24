from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil

from PIL import Image, ImageOps, ImageStat
import pytesseract


LOW_CONFIDENCE_THRESHOLD = 45.0


@dataclass(frozen=True)
class OCRResult:
    text: str
    mean_confidence: float
    word_count: int
    profile: str


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


def image_to_result(
    image_path: Path,
    lang: str = "chi_sim+eng",
    tesseract_cmd: str = "",
) -> OCRResult:
    if tesseract_cmd:
        resolved = resolve_tesseract_command(tesseract_cmd)
        if resolved is not None:
            pytesseract.pytesseract.tesseract_cmd = str(resolved)

    with Image.open(image_path) as source:
        original = ImageOps.exif_transpose(source).convert("RGB")
    primary = _recognize(original, lang, config="", profile="original")
    if not _needs_fallback(primary):
        return primary

    fallback_image = _prepare_low_confidence_fallback(original)
    fallback = _recognize(
        fallback_image,
        lang,
        config="--psm 6",
        profile="scale3_gray",
    )
    return fallback if _result_score(fallback) > _result_score(primary) else primary


def image_to_text(image_path: Path, lang: str = "chi_sim+eng", tesseract_cmd: str = "") -> str:
    return image_to_result(image_path, lang=lang, tesseract_cmd=tesseract_cmd).text


def _recognize(image: Image.Image, lang: str, config: str, profile: str) -> OCRResult:
    data = pytesseract.image_to_data(
        image,
        lang=lang,
        config=config,
        output_type=pytesseract.Output.DICT,
    )
    lines: dict[tuple[int, int, int], list[str]] = {}
    confidences: list[float] = []
    for index, raw_text in enumerate(data.get("text", [])):
        token = str(raw_text).strip()
        if not token:
            continue
        key = (
            int(data["block_num"][index]),
            int(data["par_num"][index]),
            int(data["line_num"][index]),
        )
        lines.setdefault(key, []).append(token)
        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            confidence = -1
        if confidence >= 0:
            confidences.append(confidence)

    text = "\n".join(" ".join(tokens) for tokens in lines.values()).strip()
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return OCRResult(
        text=text,
        mean_confidence=round(mean_confidence, 2),
        word_count=sum(len(tokens) for tokens in lines.values()),
        profile=profile,
    )


def _needs_fallback(result: OCRResult) -> bool:
    return result.word_count == 0 or result.mean_confidence < LOW_CONFIDENCE_THRESHOLD


def _prepare_low_confidence_fallback(image: Image.Image) -> Image.Image:
    resized = image.resize(
        (image.width * 3, image.height * 3),
        Image.Resampling.LANCZOS,
    )
    grayscale = ImageOps.autocontrast(ImageOps.grayscale(resized))
    if ImageStat.Stat(grayscale).mean[0] < 127:
        grayscale = ImageOps.invert(grayscale)
    return grayscale


def _result_score(result: OCRResult) -> float:
    return (
        result.mean_confidence
        + min(result.word_count, 20) * 0.5
        + min(len(result.text), 100) * 0.05
    )
