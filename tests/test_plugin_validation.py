from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from group_essence_extractor.db import EssenceRepository
from group_essence_extractor.models import EssenceMessage
from group_essence_extractor.plugin_config import PluginSettings
from group_essence_extractor.plugin_service import (
    GroupEssencePluginService,
    RuntimeStatusReport,
    StatusReport,
    build_validation_report,
    format_status_report,
    format_validation_report,
)


class FakeEvent:
    def __init__(self, sender_id: str, group_id: str | None) -> None:
        self.sender_id = sender_id
        self.group_id = group_id

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_group_id(self) -> str | None:
        return self.group_id


class FakeSource:
    def __init__(self, messages: list[EssenceMessage]) -> None:
        self.messages = messages
        self.calls: list[str] = []
        self.detail_limits: list[int | None] = []

    async def get_essence_messages(
        self,
        _: object,
        group_id: str,
        *,
        detail_request_limit: int | None = None,
    ) -> list[EssenceMessage]:
        self.calls.append(group_id)
        self.detail_limits.append(detail_request_limit)
        return self.messages


def make_message() -> EssenceMessage:
    return EssenceMessage(
        sender="脱敏发送者",
        sender_time="2026-08-24 20:00:00",
        essence_time="2026-08-24 20:05:00",
        operator="脱敏管理员",
        content_text="不应进入验收输出 https://private.invalid",
        content_type="text",
        group_id="123456",
        message_id="m-1",
        source="onebot",
        raw_data={
            "essence": {
                "message_id": "m-1",
                "sender_time": 1_700_000_000,
                "operator_time": 1_700_000_100,
                "content": [{"type": "text", "data": {"text": "不输出"}}],
            }
        },
    )


class PluginValidationTests(unittest.TestCase):
    def test_configuration_enforces_admin_and_group_allowlists(self) -> None:
        settings = PluginSettings.from_mapping(
            {
                "admin_ids": ["admin"],
                "allowed_group_ids": ["123456"],
                "default_group_id": "123456",
                "max_validation_detail_requests": 100,
                "max_sync_detail_requests": 100,
                "history_query_limit": 999,
                "max_query_results": 100,
            }
        )

        self.assertIsNone(
            settings.resolve_authorized_group(FakeEvent("member", "123456"))
        )
        self.assertIsNone(
            settings.resolve_authorized_group(FakeEvent("admin", "999999"))
        )
        self.assertEqual(
            settings.resolve_authorized_group(FakeEvent("admin", None)),
            "123456",
        )
        self.assertEqual(settings.max_query_results, 20)
        self.assertEqual(settings.max_validation_detail_requests, 50)
        self.assertEqual(settings.max_sync_detail_requests, 50)
        self.assertEqual(settings.history_query_limit, 500)

    def test_background_configuration_is_safe_by_default_and_clamped(self) -> None:
        defaults = PluginSettings.from_mapping({})
        self.assertEqual(defaults.scheduled_sync_block_reason, "disabled")
        self.assertFalse(defaults.background_work_enabled)

        validation = PluginSettings.from_mapping(
            {
                "validation_mode": True,
                "enable_scheduled_sync": True,
                "enable_automatic_backups": True,
                "allowed_group_ids": ["123456"],
                "onebot_platform_id": "platform-1",
            }
        )
        self.assertEqual(validation.scheduled_sync_block_reason, "validation_mode")
        self.assertFalse(validation.background_work_enabled)

        enabled = PluginSettings.from_mapping(
            {
                "validation_mode": False,
                "enable_scheduled_sync": True,
                "allowed_group_ids": ["123456"],
                "onebot_platform_id": "platform-1",
                "scheduled_sync_interval_minutes": 1,
                "scheduled_sync_timeout_seconds": 9999,
                "scheduled_sync_jitter_percent": 99,
                "scheduled_sync_failure_threshold": 0,
                "detail_retry_base_minutes": 0,
                "detail_retry_max_hours": 999,
                "backup_keep_weekly": -1,
            }
        )
        self.assertEqual(enabled.scheduled_sync_block_reason, "")
        self.assertTrue(enabled.background_work_enabled)
        self.assertEqual(enabled.scheduled_sync_interval_minutes, 5)
        self.assertEqual(enabled.scheduled_sync_timeout_seconds, 600)
        self.assertEqual(enabled.scheduled_sync_jitter_percent, 30)
        self.assertEqual(enabled.scheduled_sync_failure_threshold, 1)
        self.assertEqual(enabled.detail_retry_base_minutes, 1)
        self.assertEqual(enabled.detail_retry_max_hours, 168)
        self.assertEqual(enabled.backup_keep_weekly, 0)

    def test_scheduled_sync_reports_missing_prerequisites(self) -> None:
        missing_groups = PluginSettings.from_mapping(
            {
                "validation_mode": False,
                "enable_scheduled_sync": True,
                "onebot_platform_id": "platform-1",
            }
        )
        self.assertEqual(
            missing_groups.scheduled_sync_block_reason,
            "missing_allowed_groups",
        )

        missing_platform = PluginSettings.from_mapping(
            {
                "validation_mode": False,
                "enable_scheduled_sync": True,
                "allowed_group_ids": ["123456"],
            }
        )
        self.assertEqual(
            missing_platform.scheduled_sync_block_reason,
            "missing_platform_id",
        )

    def test_validation_and_status_do_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "plugin_data" / "group_essence.db"
            source = FakeSource([make_message()])
            service = GroupEssencePluginService(
                source=source,  # type: ignore[arg-type]
                repository=EssenceRepository(db_path),
            )

            report = asyncio.run(service.validate(object(), "123456"))
            status = asyncio.run(service.status())

            self.assertEqual(report.collected, 1)
            self.assertEqual(report.detail_requested, 0)
            self.assertEqual(report.detail_candidates, 0)
            self.assertEqual(report.detail_skipped, 0)
            self.assertEqual(report.field_types["message_id"], "str")
            self.assertEqual(source.detail_limits, [10])
            self.assertFalse(status.database_exists)
            self.assertFalse(db_path.exists())
            output = format_validation_report(report)
            self.assertNotIn("脱敏发送者", output)
            self.assertNotIn("private.invalid", output)
            self.assertNotIn("123456", output)
            self.assertIn("目标群：已授权", output)

            status_output = format_status_report(
                status,
                validation_mode=True,
                allowed_group_count=1,
            )
            self.assertIn("只读验收", status_output)
            self.assertIn("未初始化", status_output)

    def test_validation_report_distinguishes_candidates_requests_and_skips(self) -> None:
        messages: list[EssenceMessage] = []
        for index in range(3):
            message = make_message()
            message.message_id = f"missing-{index}"
            message.content_text = "[空消息]"
            message.raw_data = {
                "essence": {
                    "message_id": message.message_id,
                    "content": [],
                }
            }
            messages.append(message)
        messages[0].raw_data = {
            **(messages[0].raw_data or {}),
            "message_detail_requested": True,
            "message_detail_error": "safe error",
        }

        report = build_validation_report(messages)

        self.assertEqual(report.detail_candidates, 3)
        self.assertEqual(report.detail_requested, 1)
        self.assertEqual(report.detail_skipped, 2)
        self.assertEqual(report.detail_failed, 1)
        self.assertIn("候选=3, 请求=1, 跳过=2, 失败=1", format_validation_report(report))

    def test_status_report_exposes_only_aggregate_runtime_and_backup_state(self) -> None:
        output = format_status_report(
            StatusReport(database_exists=True, total=3, schema_version=3),
            validation_mode=False,
            allowed_group_count=1,
            runtime=RuntimeStatusReport(
                scheduled_sync_enabled=True,
                task_running=True,
                last_success_at="2026-08-25T10:00:00.000000Z",
                next_run_at="2026-08-25T10:30:00.000000Z",
                consecutive_failures=2,
                last_error_category="timeout",
                automatic_backups_enabled=True,
                last_backup_at="2026-08-25T09:00:00.000000Z",
            ),
        )

        self.assertIn("计划同步：运行中", output)
        self.assertIn("连续失败：2", output)
        self.assertIn("最后错误类别：timeout", output)
        self.assertIn("自动备份：开启", output)
        self.assertNotIn("123456", output)
        self.assertNotIn("admin-1", output)


if __name__ == "__main__":
    unittest.main()
