"""Log the PipsFarmer reader account into Telegram once and print its session string.

    python tools/telegram_login.py

Use the Telegram account that is a member of your PipsFarmer VIP group.
Get TG_API_ID / TG_API_HASH from https://my.telegram.org -> API development tools.
"""
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = int(input("TG_API_ID: ").strip())
    api_hash = input("TG_API_HASH: ").strip()
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        await client.start()
        me = await client.get_me()
        print(f"\nLogged in as {me.first_name}. Your groups and channels:\n")
        async for d in client.iter_dialogs(limit=200):
            if d.is_group or d.is_channel:
                print(f"  {d.id:>16}  {d.name}")
        print("\nPut these in your .env file:")
        print(f"TG_API_ID={api_id}\nTG_API_HASH={api_hash}")
        print(f"TG_MAIN_SESSION={client.session.save()}")
        print("TG_VIP_CHATS=<the id of your PipsFarmer VIP group from the list above>")
        print("\nTreat TG_MAIN_SESSION like a password: it gives full access to this Telegram account.")


asyncio.run(main())
