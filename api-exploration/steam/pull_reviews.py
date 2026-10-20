"""Fetch one small page of reviews; no API key or third-party packages."""
import argparse
import json
import sys
from common import SAMPLES, pull


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--appid", type=int, default=570, help="Steam app ID (default: Dota 2)")
    parser.add_argument("--count", type=int, default=5, choices=range(1, 101), metavar="1..100")
    parser.add_argument("--cursor", default="*", help="Cursor returned by the preceding page")
    parser.add_argument("--sort", choices=["recent", "updated"], default="recent")
    args = parser.parse_args()
    params = {"appid": args.appid, "num_per_page": args.count,
              "filter": 1 if args.sort == "recent" else 2,
              "languages": ["english"], "purchase_type": 1, "cursor": args.cursor}
    result = pull("https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/",
                  {"input_json": json.dumps(params)}, SAMPLES / "reviews" / str(args.appid))
    body = result.get("response", result)
    reviews = body["reviews"]
    print(f"Reviews on this page: {len(reviews)}")
    print("Query summary:", json.dumps(body.get("query_summary", {})))
    print("Next cursor:", body.get("cursor"))
    if reviews:
        review = reviews[0]
        print("Example fields:", json.dumps({k: review.get(k) for k in
              ["recommendationid", "language", "timestamp_created", "timestamp_updated", "voted_up"]}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Pull failed: {error}", file=sys.stderr)
        sys.exit(1)
