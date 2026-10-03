import argparse
import asyncio

from app.config import Settings
from app.database import Database
from app.metadata import IMDB, Metadata
from app.telegram import Telegram, make_client


async def run(args):
    settings = Settings()
    if args.command == "url":
        print(settings.addon_url)
        return
    if args.command in {"login", "chats"}:
        client = make_client(settings)
        try:
            if args.command == "login":
                await client.start()
                print(f"Session saved to {settings.session_path}.session")
            else:
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("Run the login command first")
                async for dialog in client.iter_dialogs():
                    if dialog.is_group or dialog.is_channel:
                        print(f"{dialog.id}\t{dialog.name}")
        finally:
            await client.disconnect()
        return
    db = Database(settings.data_dir / "index.sqlite3")
    await db.open()
    metadata = Metadata(db, settings.metadata_lookup)
    try:
        if args.command == "map":
            if not IMDB.fullmatch(args.imdb_id):
                raise ValueError("Expected an IMDb ID such as tt0133093")
            if not await db.get_file(args.chat_id, args.message_id):
                raise ValueError("Index this Telegram message before mapping it")
            await db.set_override(
                args.chat_id, args.message_id, args.imdb_id, await metadata.get(args.imdb_id)
            )
            print("Mapping saved; regular Stremio movie pages can now find this file")
        elif args.command == "unmatched":
            rows = await db.execute(
                "SELECT chat_id, message_id, filename FROM files "
                "WHERE imdb_id IS NULL ORDER BY posted_at DESC"
            )
            for row in rows:
                print(f"{row['chat_id']}\t{row['message_id']}\t{row['filename']}")
        elif args.command == "sync":
            telegram = Telegram(settings, db, metadata)
            try:
                await telegram.connect()
                await telegram.sync(full=args.full)
                print("Index updated")
            finally:
                await telegram.close()
    finally:
        await metadata.close()
        await db.close()


def main():
    parser = argparse.ArgumentParser(description="Set up and manage Telegram Stremio")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("login", "chats", "url", "unmatched"):
        subparsers.add_parser(command)
    sync = subparsers.add_parser("sync")
    sync.add_argument("--full", action="store_true", help="Scan all accessible history")
    mapping = subparsers.add_parser("map")
    # Support --chat-id=-100... so argparse never mistakes a negative ID for an option.
    mapping.add_argument("--chat-id", required=True, type=int)
    mapping.add_argument("--message-id", required=True, type=int)
    mapping.add_argument("--imdb-id", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
