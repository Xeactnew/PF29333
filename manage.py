"""PipsFarmer admin tasks.

  python manage.py create-admin <username>          create an admin login (you and your partner)
  python manage.py admin-telegram <username> <chat_id>   where the bot sends that admin's alerts
  python manage.py list-admins
  python manage.py delete-admin <username>
  python manage.py create-node "<name>" [slots]          register a VPS and print its key
"""
import getpass
import sys

from sqlalchemy import select

from server import security
from server.db import Admin, SessionLocal, init_engine


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    init_engine()
    cmd = argv[1]
    with SessionLocal() as s:
        if cmd == "create-admin" and len(argv) == 3:
            if s.scalar(select(Admin).where(Admin.username == argv[2])):
                print("That admin already exists.")
                return 1
            pw = getpass.getpass("Password (min 10 chars): ")
            if len(pw) < 10 or pw != getpass.getpass("Again: "):
                print("Passwords didn't match or are too short.")
                return 1
            s.add(Admin(username=argv[2], password_hash=security.hash_password(pw)))
            s.commit()
            print(f"Admin {argv[2]} created. Log in at /admin")
        elif cmd == "admin-telegram" and len(argv) == 4:
            a = s.scalar(select(Admin).where(Admin.username == argv[2]))
            if not a:
                print("No such admin.")
                return 1
            a.telegram_chat_id = int(argv[3])
            s.commit()
            print("Saved.")
        elif cmd == "create-node" and len(argv) in (3, 4):
            from server.db import Node
            token = security.new_token()
            slots = int(argv[3]) if len(argv) == 4 else 20
            s.add(Node(name=argv[2], token_hash=security.hash_token(token), max_slots=slots))
            s.commit()
            print(f"TOKEN={token}")
        elif cmd == "list-admins":
            for a in s.scalars(select(Admin)):
                print(a.username, a.telegram_chat_id or "", a.last_login or "never logged in")
        elif cmd == "delete-admin" and len(argv) == 3:
            a = s.scalar(select(Admin).where(Admin.username == argv[2]))
            if a:
                s.delete(a)
                s.commit()
                print("Deleted.")
        else:
            print(__doc__)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
