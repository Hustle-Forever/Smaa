"""Tell me whether an Instagram token will actually work — before it goes in .env.

    python check_instagram_token.py <TOKEN>
    python check_instagram_token.py            # checks the one in backend/.env

Getting this token right means getting FOUR separate things right, and Meta
reports a failure in any of them with roughly the same unhelpful error. This
walks them in order and says which one is wrong:

  1. Is the token alive?              (expired is the common one)
  2. Which APP issued it?             -- the permission lives on the APP, not the
                                         token, so a token from the wrong app
                                         fails even while being perfectly valid
  3. Which Instagram account can it see?
  4. Does business_discovery work?    -- reading ANOTHER account's posts, which
                                         is what competitor scanning is

It prints the exact INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_ACCOUNT_ID to paste,
and never prints the token itself in full.

Read-only: it makes GET calls to the Graph API and writes nothing.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

GRAPH = "https://graph.facebook.com/v21.0"
ENV = Path(__file__).resolve().parent / ".env"

OK, BAD, WARN, INFO = "  [ok]  ", "  [X]   ", "  [!]   ", "        "


def get(path: str, **params) -> dict:
    url = f"{GRAPH}/{path}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:          # Graph puts real detail in the body
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"error": {"message": f"HTTP {e.code}"}}
    except Exception as e:                        # noqa: BLE001
        return {"error": {"message": f"{type(e).__name__}: {e}"}}


def err(d: dict) -> str:
    return str((d.get("error") or {}).get("message") or "")


def account_from_env() -> str:
    """The configured INSTAGRAM_ACCOUNT_ID, which is the account the app really
    uses. It matters on its own because a token can reach a specific account
    without that account showing up under `me/accounts`."""
    if not ENV.is_file():
        return ""
    for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("INSTAGRAM_ACCOUNT_ID="):
            return line.split("=", 1)[1].strip()
    return ""


def token_from_env() -> str:
    if not ENV.is_file():
        return ""
    for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("INSTAGRAM_ACCESS_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def main() -> int:
    token = (sys.argv[1] if len(sys.argv) > 1 else token_from_env()).strip()
    if not token:
        print("No token. Pass one as an argument, or set INSTAGRAM_ACCESS_TOKEN in .env.")
        return 2

    print(f"\nChecking token {token[:8]}...{token[-6:]}  ({len(token)} chars)\n")

    # --- 1. alive? ---------------------------------------------------------
    me = get("me", fields="id,name", access_token=token)
    if me.get("error"):
        print(BAD + "The token is not usable at all.")
        print(INFO + err(me)[:300])
        print("\n  -> It is probably expired. Generate a new one (step 2 below).")
        return 1
    print(OK + f"Alive - it belongs to '{me.get('name')}' (user {me.get('id')})")

    # --- 2. which app, and for how long? -----------------------------------
    dbg = get("debug_token", input_token=token, access_token=token).get("data") or {}
    app_name, app_id = dbg.get("application"), dbg.get("app_id")
    if app_id:
        print(OK + f"Issued by app '{app_name}' (id {app_id})")
    scopes = dbg.get("scopes") or []
    expires = dbg.get("expires_at")
    if expires == 0:
        print(OK + "Never expires (this is a Page token - the good kind)")
    elif expires:
        import datetime
        when = datetime.datetime.utcfromtimestamp(expires)
        days = (when - datetime.datetime.utcnow()).days
        line = OK if days > 14 else WARN
        print(line + f"Expires {when:%d %b %Y} ({days} days). "
                     f"{'' if days > 14 else 'Get a Page token so this stops happening.'}")
    for need in ("instagram_basic", "pages_show_list", "pages_read_engagement"):
        print((OK if need in scopes else BAD) + f"scope {need}")

    # --- 3. which Instagram account can it see? ----------------------------
    # `me/accounts` is the DISCOVERY path, not the access path. A token issued
    # to a user who administers no Page lists nothing here and can still reach a
    # specific Instagram account perfectly well — which is exactly what happened
    # with the token that fixed this: empty page list, fully working account.
    # So an empty list is a note, never a verdict; the configured id is tried
    # regardless, and only business_discovery decides.
    pages = get("me/accounts", fields="name,instagram_business_account",
                access_token=token).get("data") or []
    ig_id = ""
    for p in pages:
        ig = (p.get("instagram_business_account") or {}).get("id")
        print(INFO + f"Page '{p.get('name')}' -> instagram {ig or '(none linked)'}")
        if ig and not ig_id:
            ig_id = ig
    if not ig_id:
        print(INFO + "This user administers no Page with an Instagram account "
                     "linked - that is fine if the account id is set explicitly.")
        ig_id = account_from_env()
        if not ig_id:
            print(BAD + "...and no INSTAGRAM_ACCOUNT_ID is set, so there is "
                        "nothing to try. Set one, or use a token whose user "
                        "administers the Page.")
            return 1
        print(INFO + f"Trying INSTAGRAM_ACCOUNT_ID={ig_id} from .env instead.")

    who = get(ig_id, fields="username,followers_count,media_count", access_token=token)
    if who.get("error"):
        print(BAD + f"Cannot reach instagram account {ig_id}")
        print(INFO + err(who)[:220])
        print()
        print("  -> The token is valid but belongs to a different app or user than")
        print("     the one that owns this account. Check the app id printed above:")
        print("     two apps can share a NAME, and only one of them may be the right one.")
        return 1
    print(OK + f"Reaches instagram @{who.get('username','?')} "
               f"({who.get('followers_count','?')} followers, id {ig_id})")

    # --- 4. the one that actually matters ----------------------------------
    target = "42abudhabi"
    disc = get(ig_id, fields=f"business_discovery.username({target})"
                             "{followers_count,media_count}", access_token=token)
    if disc.get("error"):
        print(BAD + f"CANNOT read other accounts (business_discovery on @{target})")
        print(INFO + err(disc)[:220])
        print()
        if "(#10)" in err(disc) or "permission" in err(disc).lower():
            print("  -> This is the blocker. 'Instagram Public Content Access' is granted")
            print(f"     to the APP, not to the token, and app '{app_name}' does not have it.")
            print("     Competitor scanning will not work with any token from this app.")
            print("     Use a token from the app that HAD the approval, or submit this")
            print("     one for App Review.")
        return 1

    found = (disc.get("business_discovery") or {})
    print(OK + f"CAN read other accounts - @{target} has "
               f"{found.get('followers_count','?')} followers")

    print("\n  Everything works. Put these in backend/.env:\n")
    print(f"    INSTAGRAM_ACCESS_TOKEN={token[:8]}...{token[-6:]}   (the full token you passed)")
    print(f"    INSTAGRAM_ACCOUNT_ID={ig_id}")
    print("\n  Then restart the backend container.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
