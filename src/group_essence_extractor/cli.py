from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn

from .config import get_settings
from .db import EssenceRepository
from .diagnostics import run_doctor
from .exporters import export_records
from .ingest import ingest_all


SEARCH_FILTERS = (
    "sender_time",
    "essence_time",
    "sender",
    "sender_qq",
    "operator",
    "operator_qq",
    "content",
    "group_id",
    "source",
    "content_type",
    "sender_time_from",
    "sender_time_to",
    "essence_time_from",
    "essence_time_to",
)


def _add_search_arguments(parser: argparse.ArgumentParser, pagination: bool) -> None:
    parser.add_argument("--sender-time", default="")
    parser.add_argument("--essence-time", default="")
    parser.add_argument("--sender", default="")
    parser.add_argument("--sender-qq", default="")
    parser.add_argument("--operator", default="")
    parser.add_argument("--operator-qq", default="")
    parser.add_argument("--content", default="")
    parser.add_argument("--group-id", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--content-type", default="")
    parser.add_argument("--sender-time-from", default="")
    parser.add_argument("--sender-time-to", default="")
    parser.add_argument("--essence-time-from", default="")
    parser.add_argument("--essence-time-to", default="")
    if pagination:
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--offset", type=int, default=0)


def _search_filters(args: argparse.Namespace) -> dict[str, str]:
    return {name: str(getattr(args, name)) for name in SEARCH_FILTERS}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QQ群精华消息提取器")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="初始化数据库")
    ingest = sub.add_parser("ingest", help="执行一次采集/入库")
    ingest.add_argument("--dry-run", action="store_true", help="采集并检查字段，但不写数据库")

    sub.add_parser("doctor", help="检查本地配置和运行条件（不联网、不写文件）")
    sub.add_parser("audit-db", help="只读审计现有数据库的数据质量")

    repair = sub.add_parser("repair-db", help="预览或修复可从原始响应恢复的旧数据")
    repair.add_argument("--apply", action="store_true", help="实际写入；默认仅做只读预览")
    repair.add_argument("--group-id", default="", help="缺失群号的 OneBot 记录使用此值")

    search = sub.add_parser("search", help="本地搜索")
    _add_search_arguments(search, pagination=True)

    export = sub.add_parser("export", help="将筛选结果导出为 JSON 或 CSV")
    export.add_argument("--format", choices=("json", "csv"), required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--max-records", type=int)
    export.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    _add_search_arguments(export, pagination=False)

    serve = sub.add_parser("serve", help="启动远程搜索 API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = get_settings()

    if args.command == "doctor":
        report = run_doctor(settings)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["status"] == "error" else 0

    if args.command == "audit-db":
        report = EssenceRepository(settings.db_path).audit()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["status"] == "error" else 0

    if args.command == "ingest" and args.dry_run:
        stat = ingest_all(settings, dry_run=True)
        print(json.dumps(stat, ensure_ascii=False, indent=2))
        return 0

    repo = EssenceRepository(settings.db_path)

    if args.command == "init-db":
        migration = repo.init_db()
        report = {
            "status": "ok",
            "database": str(settings.db_path),
            **migration.as_dict(),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.command == "repair-db":
        migration = repo.init_db() if args.apply else None
        report = repo.repair(
            default_group_id=args.group_id or settings.group_id,
            apply=args.apply,
        )
        if migration is not None:
            report["migration"] = migration.as_dict()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["status"] == "error" else 0

    if args.command == "ingest":
        repo.init_db()
        stat = ingest_all(settings, repo)
        print(json.dumps(stat, ensure_ascii=False, indent=2))
        return 0

    if args.command == "search":
        repo.init_db()
        page = repo.search_page(
            **_search_filters(args),
            limit=args.limit,
            offset=args.offset,
        )
        print(json.dumps(page.as_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "export":
        repo.init_db()
        try:
            report = export_records(
                repository=repo,
                output_path=args.output,
                output_format=args.format,
                filters=_search_filters(args),
                max_records=args.max_records,
                force=args.force,
            )
        except (FileExistsError, ValueError) as exc:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.command == "serve":
        uvicorn.run("group_essence_extractor.api:app", host=args.host, port=args.port, reload=False)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
