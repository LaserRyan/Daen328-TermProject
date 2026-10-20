"""Fetch one small page of Steam news; no API key or third-party packages."""
import argparse
import json
import sys
from common import SAMPLES, pull


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--appid", type=int, default=570, help="Steam app ID (default: Dota 2)")
    parser.add_argument("--count", type=int, default=5, choices=range(1, 101), metavar="1..100")
    parser.add_argument("--enddate", type=int, help="Fetch posts before this Unix timestamp")
    args = parser.parse_args()
    params = {"appid": args.appid, "count": args.count, "maxlength": 0}
    if args.enddate is not None:
        params["enddate"] = args.enddate
    result = pull("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
                  params, SAMPLES / "news" / str(args.appid))
    items = result["appnews"]["newsitems"]
    print(f"News posts on this page: {len(items)}")
    if items:
        print("Example fields:", json.dumps({k: items[0].get(k) for k in
              ["gid", "appid", "title", "date", "feedlabel"]}, ensure_ascii=True))
        print("Oldest date on page:", min(item["date"] for item in items))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Pull failed: {error}", file=sys.stderr)
        sys.exit(1)
