from __future__ import annotations

import argparse
import json

import uvicorn

from .config import get_settings
from .db import EssenceRepository
from .diagnostics import run_doctor
from .ingest import ingest_all


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QQ群精华消息提取器")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="初始化数据库")
    ingest = sub.add_parser("ingest", help="执行一次采集/入库")
    ingest.add_argument("--dry-run", action="store_true", help="采集并检查字段，但不写数据库")

    sub.add_parser("doctor", help="检查本地配置和运行条件（不联网、不写文件）")
    sub.add_parser("audit-db", help="只读审计现有数据库的数据质量")

    search = sub.add_parser("search", help="本地搜索")
    search.add_argument("--sender-time", default="")
    search.add_argument("--essence-time", default="")
    search.add_argument("--sender", default="")
    search.add_argument("--sender-qq", default="")
    search.add_argument("--operator", default="")
    search.add_argument("--operator-qq", default="")
    search.add_argument("--content", default="")
    search.add_argument("--limit", type=int, default=100)
    search.add_argument("--offset", type=int, default=0)

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
        repo.init_db()
        print(f"数据库已初始化: {settings.db_path}")
        return 0

    if args.command == "ingest":
        repo.init_db()
        stat = ingest_all(settings, repo)
        print(json.dumps(stat, ensure_ascii=False, indent=2))
        return 0

    if args.command == "search":
        repo.init_db()
        items = repo.search(
            sender_time=args.sender_time,
            essence_time=args.essence_time,
            sender=args.sender,
            sender_qq=args.sender_qq,
            operator=args.operator,
            operator_qq=args.operator_qq,
            content=args.content,
            limit=args.limit,
            offset=args.offset,
        )
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0

    if args.command == "serve":
        uvicorn.run("group_essence_extractor.api:app", host=args.host, port=args.port, reload=False)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
