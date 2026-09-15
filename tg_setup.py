#!/usr/bin/env python3
"""Find your Telegram chat id and prove the bot can reach it.

Run:  python3 tg_setup.py                      (prompts, hidden input)
      python3 tg_setup.py --token-file PATH    (no terminal needed)
      TELEGRAM_BOT_TOKEN=... python3 tg_setup.py

The token is never echoed, never written to disk, and never printed back.
Nothing here modifies the repo.
"""

import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from check import SSL_CONTEXT  # reuse the CA-bundle fallback

API = "https://api.telegram.org/bot%s/%s"
# my_chat_member fires the moment a bot is added to a group, so the chat id is
# discoverable even if no message of yours ever reached the bot.
WANTED = '["message","edited_message","channel_post","my_chat_member","chat_member"]'


def call(token, method, **params):
    data = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(API % (token, method), data=data)
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8", "replace"))
        except ValueError:
            return {"ok": False, "description": "HTTP %s" % exc.code}
    except Exception as exc:
        return {"ok": False, "description": str(exc)}


def chats_in(updates):
    """Every distinct chat visible in any update type, newest first."""
    found, order = {}, []
    for update in updates:
        for value in update.values():
            if isinstance(value, dict) and isinstance(value.get("chat"), dict):
                chat = value["chat"]
                key = chat.get("id")
                if key is not None and key not in found:
                    found[key] = chat
                    order.append(key)
    return [found[k] for k in order]


def read_token(argv):
    """Token from --token-file, the environment, or a hidden prompt."""
    if "--token-file" in argv:
        path = os.path.expanduser(argv[argv.index("--token-file") + 1])
        with open(path) as handle:
            return handle.read().strip()

    from_env = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if from_env:
        return from_env

    # A hidden prompt needs a real terminal. Claude Code's "!" mode and most CI
    # runners have no TTY, where getpass would either echo the token or blow up.
    if not sys.stdin.isatty():
        print("No terminal available for a hidden prompt, and the token must not")
        print("be typed anywhere it would be echoed. Pick one:\n")
        print("  a) Run this in Terminal.app instead:")
        print("       cd ~/projects/monitor-tcf && python3 tg_setup.py\n")
        print("  b) Put the token in a file, then point at it:")
        print("       (create ~/.tcf_bot_token in a text editor, paste, save)")
        print("       chmod 600 ~/.tcf_bot_token")
        print("       python3 tg_setup.py --token-file ~/.tcf_bot_token")
        return None

    print("Paste your bot token from @BotFather (input stays hidden):")
    sys.stdout.flush()
    return getpass.getpass("  token: ").strip()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        token = read_token(argv)
    except (OSError, IndexError) as exc:
        print("could not read the token: %s" % exc)
        return 2
    if token is None:
        return 2
    if not token:
        print("no token given")
        return 2

    # 1. Is the token even valid?
    me = call(token, "getMe")
    if not me.get("ok"):
        print("\n✗ The token is not valid: %s" % me.get("description"))
        print("  Re-copy it from @BotFather - it looks like 123456789:AA... with no spaces.")
        return 1
    bot = me["result"]
    print("\n✓ Token works. Bot is @%s (%s)" % (bot.get("username"), bot.get("first_name")))

    # 2. A webhook silently swallows every update getUpdates would return.
    hook = call(token, "getWebhookInfo")
    hook_url = (hook.get("result") or {}).get("url") or ""
    if hook_url:
        print("\n! A webhook is set (%s) - it consumes updates before getUpdates sees them." % hook_url)
        if input("  Delete it? [y/N] ").strip().lower().startswith("y"):
            print("  %s" % ("deleted" if call(token, "deleteWebhook").get("ok") else "could not delete"))

    # 3. What can the bot actually see?
    updates = call(token, "getUpdates", timeout=0, allowed_updates=WANTED)
    if not updates.get("ok"):
        print("\n✗ getUpdates failed: %s" % updates.get("description"))
        return 1

    chats = chats_in(updates.get("result") or [])
    if not chats:
        print("\n✗ The bot has not seen any chat yet. In order of likelihood:\n")
        print("  1. Privacy mode. Bots only see messages starting with '/'.")
        print("     In the group send:  /start@%s" % bot.get("username"))
        print("     (the @%s part removes all doubt)" % bot.get("username"))
        print("  2. Wrong group. Confirm @%s is in the member list." % bot.get("username"))
        print("  3. Updates expire after 24 h. Send the command again, then re-run this.")
        print("  4. Still nothing? Message the bot DIRECTLY with /start - alerts can go")
        print("     to you privately, and you can switch to a group later.")
        print("\n  Then re-run:  python3 tg_setup.py")
        return 1

    print("\n✓ Found %d chat(s):\n" % len(chats))
    for i, chat in enumerate(chats, 1):
        name = chat.get("title") or " ".join(
            filter(None, [chat.get("first_name"), chat.get("last_name")])
        ) or chat.get("username") or "(no name)"
        print("  [%d] %-38s  type=%-10s id=%s" % (i, name[:38], chat.get("type"), chat["id"]))

    print("\n  A group id is negative; your own DM id is positive.")
    choice = input("\nSend a test message to which one? [number, or Enter to skip] ").strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(chats)):
        print("\nSkipped. Use the id above as TELEGRAM_CHAT_ID.")
        return 0

    target = chats[int(choice) - 1]
    sent = call(
        token, "sendMessage", chat_id=target["id"], parse_mode="HTML",
        text="✅ <b>TCF watcher</b> can reach this chat.",
    )
    if sent.get("ok"):
        print("\n✓ Delivered. Check Telegram.")
        print("\nNow run these two, pasting the token and this id when prompted:")
        print("    gh secret set TELEGRAM_BOT_TOKEN")
        print("    gh secret set TELEGRAM_CHAT_ID     ->  %s" % target["id"])
        return 0

    print("\n✗ Could not send: %s" % sent.get("description"))
    moved = (sent.get("parameters") or {}).get("migrate_to_chat_id")
    if moved:
        print("  The group became a supergroup. Use this id instead: %s" % moved)
    return 1


if __name__ == "__main__":
    sys.exit(main())
