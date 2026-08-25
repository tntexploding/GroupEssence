from __future__ import annotations

import asyncio
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from typing import Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeLogger:
    def info(self, _: str) -> None:
        return None

    def warning(self, _: str) -> None:
        return None

    def error(self, _: str) -> None:
        return None


class FakeFilter:
    class PlatformAdapterType:
        AIOCQHTTP = "aiocqhttp"

    @staticmethod
    def command(_: str):
        return lambda function: function

    @staticmethod
    def platform_adapter_type(_: object):
        return lambda function: function


class FakeStar:
    def __init__(self, context: object) -> None:
        self.context = context


class FakeEvent:
    def __init__(self, sender_id: str = "admin", group_id: str | None = "123456") -> None:
        self.sender_id = sender_id
        self.group_id = group_id
        self.stop_calls = 0

    def stop_event(self) -> None:
        self.stop_calls += 1

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_group_id(self) -> str | None:
        return self.group_id

    def plain_result(self, text: str) -> str:
        return text


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def validate(self, _: object, __: str) -> SimpleNamespace:
        self.calls.append(("validate", __))
        return SimpleNamespace(
            collected=1,
            by_content_type={"text": 1},
            missing={
                "message_id": 0,
                "sender_time": 0,
                "essence_time": 0,
                "content": 0,
            },
            field_types={
                "message_id": "str",
                "sender_time": "int",
                "essence_time": "int",
                "content": "list",
            },
            detail_requested=0,
            detail_failed=0,
        )

    async def status(self) -> SimpleNamespace:
        self.calls.append(("status",))
        return SimpleNamespace(database_exists=False, total=0, schema_version=None)

    async def sync(self, _: object, group_id: str) -> SimpleNamespace:
        self.calls.append(("sync", group_id))
        return SimpleNamespace(collected=1, inserted=1, updated=0, unchanged=0)

    async def search(self, group_id: str, keyword: str, limit: int) -> SimpleNamespace:
        self.calls.append(("search", group_id, keyword, limit))
        return SimpleNamespace(items=[], total=0, limit=limit, offset=0)

    async def recent(self, group_id: str, limit: int) -> SimpleNamespace:
        self.calls.append(("recent", group_id, limit))
        return SimpleNamespace(items=[], total=0, limit=limit, offset=0)


@contextmanager
def load_plugin_main(data_root: Path) -> Iterator[ModuleType]:
    module_names = (
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.star",
        "astrbot.core",
        "astrbot.core.utils",
        "astrbot.core.utils.astrbot_path",
    )
    original_modules = {name: sys.modules.get(name) for name in module_names}

    astrbot = ModuleType("astrbot")
    astrbot.__path__ = []  # type: ignore[attr-defined]
    api = ModuleType("astrbot.api")
    api.__path__ = []  # type: ignore[attr-defined]
    api.AstrBotConfig = dict  # type: ignore[attr-defined]
    api.logger = FakeLogger()  # type: ignore[attr-defined]
    event = ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object  # type: ignore[attr-defined]
    event.filter = FakeFilter()  # type: ignore[attr-defined]
    star = ModuleType("astrbot.api.star")
    star.Context = object  # type: ignore[attr-defined]
    star.Star = FakeStar  # type: ignore[attr-defined]
    core = ModuleType("astrbot.core")
    core.__path__ = []  # type: ignore[attr-defined]
    utils = ModuleType("astrbot.core.utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    path_api = ModuleType("astrbot.core.utils.astrbot_path")
    path_api.get_astrbot_data_path = lambda: str(data_root)  # type: ignore[attr-defined]

    replacements = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.core": core,
        "astrbot.core.utils": utils,
        "astrbot.core.utils.astrbot_path": path_api,
    }
    sys.modules.update(replacements)

    package_name = "_group_essence_plugin_test"
    package = ModuleType(package_name)
    package.__path__ = [str(PROJECT_ROOT)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    module_name = f"{package_name}.main"
    spec = importlib.util.spec_from_file_location(module_name, PROJECT_ROOT / "main.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载插件入口测试规格")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        for name in list(sys.modules):
            if name == package_name or name.startswith(f"{package_name}."):
                sys.modules.pop(name, None)
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


async def collect_results(generator: object) -> list[str]:
    return [item async for item in generator]  # type: ignore[attr-defined]


class PluginEntryTests(unittest.TestCase):
    def test_initialization_and_validation_command_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp)
            with load_plugin_main(data_root) as plugin_main:
                plugin = plugin_main.GroupEssencePlugin(
                    object(),
                    {
                        "admin_ids": ["admin"],
                        "allowed_group_ids": ["123456"],
                        "validation_mode": True,
                    },
                )
                self.assertFalse((data_root / "plugin_data").exists())
                plugin.service = FakeService()
                event = FakeEvent()

                results = asyncio.run(collect_results(plugin.validate_essence(event)))

                self.assertEqual(event.stop_calls, 1)
                self.assertEqual(len(results), 1)
                self.assertIn("精华验收成功", results[0])
                self.assertNotIn("123456", results[0])
                self.assertFalse((data_root / "plugin_data").exists())

    def test_permission_denials_and_status_always_stop_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with load_plugin_main(Path(temp)) as plugin_main:
                plugin = plugin_main.GroupEssencePlugin(
                    object(),
                    {
                        "admin_ids": ["admin"],
                        "allowed_group_ids": ["123456"],
                    },
                )
                plugin.service = FakeService()
                denied = FakeEvent(sender_id="member")
                status_denied = FakeEvent(sender_id="member")

                validate_results = asyncio.run(
                    collect_results(plugin.validate_essence(denied))
                )
                status_results = asyncio.run(
                    collect_results(plugin.essence_status(status_denied))
                )

                self.assertEqual(denied.stop_calls, 1)
                self.assertEqual(status_denied.stop_calls, 1)
                self.assertEqual(validate_results, ["无权限或目标群未在允许列表中。"])
                self.assertEqual(status_results, ["无权限。"])

    def test_validation_mode_blocks_sync_and_queries_before_service_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp)
            with load_plugin_main(data_root) as plugin_main:
                plugin = plugin_main.GroupEssencePlugin(
                    object(),
                    {
                        "admin_ids": ["admin"],
                        "allowed_group_ids": ["123456"],
                        "validation_mode": True,
                    },
                )
                service = FakeService()
                plugin.service = service
                sync_event = FakeEvent()
                search_event = FakeEvent()
                recent_event = FakeEvent()

                sync_results = asyncio.run(
                    collect_results(plugin.sync_essence(sync_event))
                )
                search_results = asyncio.run(
                    collect_results(plugin.search_essence(search_event, "关键词"))
                )
                recent_results = asyncio.run(
                    collect_results(plugin.recent_essence(recent_event, "3"))
                )

                self.assertEqual(sync_event.stop_calls, 1)
                self.assertEqual(search_event.stop_calls, 1)
                self.assertEqual(recent_event.stop_calls, 1)
                self.assertTrue(all("只读验收模式" in item[0] for item in (
                    sync_results,
                    search_results,
                    recent_results,
                )))
                self.assertEqual(service.calls, [])
                self.assertFalse((data_root / "plugin_data").exists())

    def test_enabled_commands_stop_events_and_use_current_authorized_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with load_plugin_main(Path(temp)) as plugin_main:
                plugin = plugin_main.GroupEssencePlugin(
                    object(),
                    {
                        "admin_ids": ["admin"],
                        "allowed_group_ids": ["123456", "654321"],
                        "validation_mode": False,
                        "max_query_results": 4,
                    },
                )
                service = FakeService()
                plugin.service = service
                sync_event = FakeEvent()
                search_event = FakeEvent()
                recent_event = FakeEvent()

                sync_results = asyncio.run(
                    collect_results(plugin.sync_essence(sync_event, "654321"))
                )
                search_results = asyncio.run(
                    collect_results(plugin.search_essence(search_event, "活动"))
                )
                recent_results = asyncio.run(
                    collect_results(plugin.recent_essence(recent_event, "2"))
                )

                self.assertEqual(sync_event.stop_calls, 1)
                self.assertEqual(search_event.stop_calls, 1)
                self.assertEqual(recent_event.stop_calls, 1)
                self.assertIn("精华同步完成", sync_results[0])
                self.assertIn("未找到匹配记录", search_results[0])
                self.assertIn("未找到匹配记录", recent_results[0])
                self.assertEqual(
                    service.calls,
                    [
                        ("sync", "654321"),
                        ("search", "123456", "活动", 4),
                        ("recent", "123456", 2),
                    ],
                )

    def test_invalid_query_arguments_stop_event_without_service_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with load_plugin_main(Path(temp)) as plugin_main:
                plugin = plugin_main.GroupEssencePlugin(
                    object(),
                    {
                        "admin_ids": ["admin"],
                        "allowed_group_ids": ["123456"],
                        "validation_mode": False,
                    },
                )
                service = FakeService()
                plugin.service = service
                search_event = FakeEvent()
                recent_event = FakeEvent()

                search_results = asyncio.run(
                    collect_results(plugin.search_essence(search_event, "   "))
                )
                recent_results = asyncio.run(
                    collect_results(plugin.recent_essence(recent_event, "21"))
                )

                self.assertEqual(search_event.stop_calls, 1)
                self.assertEqual(recent_event.stop_calls, 1)
                self.assertEqual(search_results, ["查询关键词不能为空。"])
                self.assertEqual(recent_results, ["数量必须是 1 到 20 之间的整数。"])
                self.assertEqual(service.calls, [])

    def test_all_commands_stop_event_when_dependencies_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with load_plugin_main(Path(temp)) as plugin_main:
                class FailingService:
                    async def validate(self, _: object, __: str) -> object:
                        raise plugin_main.OneBotActionError(
                            "get_essence_msg_list",
                            status="failed",
                            retcode=100,
                        )

                    async def status(self) -> object:
                        raise plugin_main.PluginServiceError(
                            "数据库状态读取失败。",
                            "sqlite_error",
                        )

                    async def sync(self, _: object, __: str) -> object:
                        raise plugin_main.PluginServiceError(
                            "数据库同步失败。",
                            "sqlite_error",
                        )

                    async def search(self, _: str, __: str, ___: int) -> object:
                        raise plugin_main.PluginServiceError(
                            "数据库查询失败。",
                            "sqlite_error",
                        )

                    async def recent(self, _: str, __: int) -> object:
                        raise plugin_main.PluginServiceError(
                            "数据库查询失败。",
                            "sqlite_error",
                        )

                plugin = plugin_main.GroupEssencePlugin(
                    object(),
                    {
                        "admin_ids": ["admin"],
                        "allowed_group_ids": ["123456"],
                        "validation_mode": False,
                    },
                )
                plugin.service = FailingService()
                events = [FakeEvent() for _ in range(5)]

                results = [
                    asyncio.run(collect_results(plugin.validate_essence(events[0]))),
                    asyncio.run(collect_results(plugin.essence_status(events[1]))),
                    asyncio.run(collect_results(plugin.sync_essence(events[2]))),
                    asyncio.run(
                        collect_results(plugin.search_essence(events[3], "关键词"))
                    ),
                    asyncio.run(collect_results(plugin.recent_essence(events[4], "5"))),
                ]

                self.assertTrue(all(event.stop_calls == 1 for event in events))
                self.assertTrue(all(len(result) == 1 for result in results))
                self.assertTrue(all("失败" in result[0] for result in results))


if __name__ == "__main__":
    unittest.main()
