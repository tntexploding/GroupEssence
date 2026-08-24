from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from group_essence_extractor.ocr import image_to_result, image_to_text


def ocr_data(lines: list[list[tuple[str, float]]]) -> dict[str, list[object]]:
    result: dict[str, list[object]] = {
        "text": [],
        "conf": [],
        "block_num": [],
        "par_num": [],
        "line_num": [],
    }
    for line_number, tokens in enumerate(lines, start=1):
        for text, confidence in tokens:
            result["text"].append(text)
            result["conf"].append(str(confidence))
            result["block_num"].append(1)
            result["par_num"].append(1)
            result["line_num"].append(line_number)
    return result


class OCRTests(unittest.TestCase):
    def _make_image(self, root: Path) -> Path:
        image_path = root / "input.png"
        Image.new("RGB", (12, 8), "white").save(image_path)
        return image_path

    def test_high_confidence_original_is_returned_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            image_path = self._make_image(Path(temp))
            recognized = ocr_data(
                [[("发送者", 82), ("测试用户", 78)], [("正文", 90)]]
            )
            with patch(
                "group_essence_extractor.ocr.pytesseract.image_to_data",
                return_value=recognized,
            ) as recognize:
                result = image_to_result(image_path)

            self.assertEqual(recognize.call_count, 1)
            self.assertEqual(result.text, "发送者 测试用户\n正文")
            self.assertEqual(result.word_count, 3)
            self.assertEqual(result.mean_confidence, 83.33)
            self.assertEqual(result.profile, "original")

    def test_low_confidence_original_uses_better_grayscale_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            image_path = self._make_image(Path(temp))
            primary = ocr_data([[("模糊", 20)]])
            fallback = ocr_data([[("清晰", 88), ("正文", 92)]])
            with patch(
                "group_essence_extractor.ocr.pytesseract.image_to_data",
                side_effect=[primary, fallback],
            ) as recognize:
                result = image_to_result(image_path)

            self.assertEqual(recognize.call_count, 2)
            fallback_image = recognize.call_args_list[1].args[0]
            self.assertEqual(fallback_image.size, (36, 24))
            self.assertEqual(fallback_image.mode, "L")
            self.assertEqual(result.text, "清晰 正文")
            self.assertEqual(result.mean_confidence, 90.0)
            self.assertEqual(result.profile, "scale3_gray")

    def test_text_wrapper_preserves_the_existing_string_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            image_path = self._make_image(Path(temp))
            with patch(
                "group_essence_extractor.ocr.pytesseract.image_to_data",
                return_value=ocr_data([[("正文", 90)]]),
            ):
                self.assertEqual(image_to_text(image_path), "正文")


if __name__ == "__main__":
    unittest.main()
