"""命令行入口：运行一条最小报价链路。"""

from __future__ import annotations

import argparse
from pathlib import Path



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hvac-quote",
        description="暖通工程图纸辅助报价工作台",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="启动本地报价页面")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--runs", type=Path, default=Path("runs"))
    serve_parser.add_argument("--data", type=Path, default=Path("data"))

    user_add_parser = subparsers.add_parser(
        "user-add", help="在本机创建报价系统账号"
    )
    user_add_parser.add_argument("--username", required=True)
    user_add_parser.add_argument("--display-name", required=True)
    user_add_parser.add_argument("--password", default=None)
    user_add_parser.add_argument("--data", type=Path, default=Path("data"))

    user_list_parser = subparsers.add_parser(
        "user-list", help="列出本机账号"
    )
    user_list_parser.add_argument("--data", type=Path, default=Path("data"))

    user_password_parser = subparsers.add_parser(
        "user-set-password", help="重置本机账号密码"
    )
    user_password_parser.add_argument("--username", required=True)
    user_password_parser.add_argument("--password", default=None)
    user_password_parser.add_argument("--data", type=Path, default=Path("data"))

    backup_parser = subparsers.add_parser(
        "backup", help="备份本地 SQLite 数据库"
    )
    backup_parser.add_argument("--data", type=Path, default=Path("data"))
    backup_parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="备份文件路径；默认 data/backup-YYYYmmdd-HHMMSS.db",
    )

    args = parser.parse_args(argv)
    if args.command == "serve":
        import uvicorn

        from .app import create_app

        uvicorn.run(
            create_app(args.runs, data_dir=args.data),
            host=args.host,
            port=args.port,
        )
        return 0
    if args.command == "user-add":
        import getpass

        from .auth import AuthError, AuthStore

        password = args.password
        if password is None:
            password = getpass.getpass("密码（至少 8 位）: ")
            confirm = getpass.getpass("再输入一次: ")
            if password != confirm:
                print("两次输入的密码不一致")
                return 2
        try:
            user = AuthStore(args.data).create_user(
                args.username, password, args.display_name
            )
        except AuthError as exc:
            print(f"创建失败：{exc}")
            return 2
        print(f"已创建账号：{user.username}（{user.display_name}）")
        return 0
    if args.command == "user-list":
        from .auth import AuthStore

        users = AuthStore(args.data).list_users()
        if not users:
            print("还没有账号")
            return 0
        for user in users:
            role = "（管理员）" if user.is_admin else ""
            print(f"{user.username}\t{user.display_name}{role}\t{user.created_at}")
        return 0
    if args.command == "user-set-password":
        import getpass

        from .auth import AuthError, AuthStore

        password = args.password
        if password is None:
            password = getpass.getpass("新密码（至少 8 位）: ")
            confirm = getpass.getpass("再输入一次: ")
            if password != confirm:
                print("两次输入的密码不一致")
                return 2
        try:
            AuthStore(args.data).set_password(args.username, password)
        except AuthError as exc:
            print(f"重置失败：{exc}")
            return 2
        print(f"已重置 {args.username} 的密码")
        return 0
    if args.command == "backup":
        from datetime import datetime

        from .db import Database

        source = args.data / "hvac.db"
        if not source.is_file():
            print(f"数据库不存在：{source}")
            return 2
        target = args.out or (
            args.data / f"backup-{datetime.now():%Y%m%d-%H%M%S}.db"
        )
        Database(source).backup_to(target)
        print(f"已备份：{target}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
