from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from group_essence_extractor.config import Settings
from group_essence_extractor.diagnostics import run_doctor


def make_settings(root: Path, **overrides: object) -> Settings:
    values = {
        "db_path": root / "data" / "database.db",
        "onebot_base_url": "http://127.0.0.1:3000",
        "onebot_access_token": "",
        "group_id": "123456",
        "prefer_onebot": True,
        "fallback_ocr": False,
        "ocr_lang": "chi_sim+eng",
        "tesseract_cmd": "",
        "screenshot_dir": root / "screenshots",
        "image_dir": root / "images",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class DoctorTests(unittest.TestCase):
    def test_valid_onebot_configuration_is_ready_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = run_doctor(make_settings(Path(temp)))

        self.assertEqual(report["status"], "ok")
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["onebot_config"]["status"], "ok")
        self.assertIn("未联网检查", checks["onebot_config"]["message"])

    def test_missing_group_id_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = run_doctor(make_settings(Path(temp), group_id=""))

        self.assertEqual(report["status"], "error")
        checks = {check["name"]: check for check in report["checks"]}
        self.assertIn("GROUP_ID", checks["onebot_config"]["message"])

    def test_ocr_files_are_checked_without_running_tesseract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / ("tesseract.exe" if os.name == "nt" else "tesseract")
            executable.write_bytes(b"fixture")
            screenshot_dir = root / "screenshots"
            screenshot_dir.mkdir()
            (screenshot_dir / "sample.png").write_bytes(b"fixture")
            report = run_doctor(
                make_settings(
                    root,
                    prefer_onebot=False,
                    fallback_ocr=True,
                    tesseract_cmd=str(executable),
                    screenshot_dir=screenshot_dir,
                )
            )

        self.assertEqual(report["status"], "ok")
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["tesseract"]["status"], "ok")
        self.assertIn("1 个候选文件", checks["screenshot_dir"]["message"])

    def test_disabling_all_sources_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = run_doctor(
                make_settings(Path(temp), prefer_onebot=False, fallback_ocr=False)
            )

        self.assertEqual(report["status"], "error")
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["ingest_source"]["status"], "error")

    def test_image_enrichment_checks_tesseract_and_cache_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / ("tesseract.exe" if os.name == "nt" else "tesseract")
            executable.write_bytes(b"fixture")
            report = run_doctor(
                make_settings(
                    root,
                    fallback_ocr=False,
                    tesseract_cmd=str(executable),
                ),
                require_image_enrichment=True,
            )

        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["tesseract"]["status"], "ok")
        self.assertEqual(checks["image_dir"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
