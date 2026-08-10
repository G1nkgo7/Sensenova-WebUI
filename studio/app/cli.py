"""Admin CLI (stdlib only).

  uv run python -m app.cli create-admin --username admin --password 'pw' --name 管理员
  uv run python -m app.cli list-users

Regular users self-register at /register; this CLI is only for bootstrapping an
admin account or inspecting users.
"""
import argparse

from . import auth
from .db import connect, init_db


def create_admin(args):
    init_db()
    con = connect()
    try:
        auth.create_user(con, args.username.strip(), args.password, role="admin", display_name=args.name)
        print("已创建管理员:", args.username)
    except auth.UsernameTaken:
        print("用户名已存在:", args.username)


def list_users(args):
    init_db()
    con = connect()
    rows = con.execute(
        "SELECT id,username,role,is_active,created_at FROM users ORDER BY id"
    ).fetchall()
    if not rows:
        print("(无用户)")
    for r in rows:
        print("#%d  %-24s  %-6s  %s" % (
            r["id"], r["username"], r["role"], "active" if r["is_active"] else "disabled"))


def main():
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("create-admin", help="创建管理员账号 (bootstrap)")
    a.add_argument("--username", required=True)
    a.add_argument("--password", required=True)
    a.add_argument("--name", default="")
    a.set_defaults(func=create_admin)

    l = sub.add_parser("list-users", help="列出用户")
    l.set_defaults(func=list_users)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
