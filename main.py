from __future__ import annotations

from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .src.group_essence_extractor.astrbot_source import (
    AstrBotEssenceSource,
    OneBotActionError,
)
from .src.group_essence_extractor.db import EssenceRepository
from .src.group_essence_extractor.plugin_config import PluginSettings
from .src.group_essence_extractor.plugin_service import (
    GroupEssencePluginService,
    PluginServiceError,
    format_status_report,
    format_validation_report,
)


PLUGIN_DATA_DIR = "astrbot_plugin_group_essence"


class GroupEssencePlugin(Star):
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        super().__init__(context)
        self.settings = PluginSettings.from_mapping(config)
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_DATA_DIR
        self.service = GroupEssencePluginService(
            source=AstrBotEssenceSource(),
            repository=EssenceRepository(data_dir / "group_essence.db"),
        )

    @filter.command("精华验收")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def validate_essence(
        self,
        event: AstrMessageEvent,
        group_id: str = "",
    ):
        """只读检查当前 OneBot 精华消息契约，不写数据库。"""
        event.stop_event()
        target = self.settings.resolve_authorized_group(event, group_id)
        if target is None:
            yield event.plain_result("无权限或目标群未在允许列表中。")
            return

        try:
            report = await self.service.validate(event, target)
        except OneBotActionError as exc:
            logger.warning(f"GroupEssence 验收失败：{exc.public_message}")
            yield event.plain_result(f"精华验收失败：{exc.public_message}")
            return
        except PluginServiceError as exc:
            logger.warning(f"GroupEssence 验收失败：category={exc.category}")
            yield event.plain_result(f"精华验收失败：{exc.public_message}")
            return
        except Exception as exc:
            logger.error(f"GroupEssence 验收失败：category={type(exc).__name__}")
            yield event.plain_result("精华验收失败：内部错误。")
            return

        logger.info(f"GroupEssence 验收完成：collected={report.collected}")
        yield event.plain_result(format_validation_report(report))

    @filter.command("精华状态")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def essence_status(self, event: AstrMessageEvent):
        """显示脱敏后的插件模式和数据库状态。"""
        event.stop_event()
        if not self.settings.is_admin(event):
            yield event.plain_result("无权限。")
            return

        try:
            report = await self.service.status()
        except PluginServiceError as exc:
            logger.warning(f"GroupEssence 状态读取失败：category={exc.category}")
            yield event.plain_result(f"精华状态读取失败：{exc.public_message}")
            return
        except Exception as exc:
            logger.error(f"GroupEssence 状态读取失败：category={type(exc).__name__}")
            yield event.plain_result("精华状态读取失败：内部错误。")
            return

        yield event.plain_result(
            format_status_report(
                report,
                validation_mode=self.settings.validation_mode,
                allowed_group_count=len(self.settings.allowed_group_ids),
            )
        )

    async def terminate(self) -> None:
        return None
