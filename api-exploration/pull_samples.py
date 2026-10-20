"""Pull a small sample from YouTube, Wikipedia recent changes, and Stack Exchange.

Usage (from repo root):
    python api-exploration/pull_samples.py              # all sources
    python api-exploration/pull_samples.py wikipedia    # one or more of: youtube wikipedia stackexchange

Keys are read from .env in the repo root. Output goes to api-exploration/<source>/samples/.
"""
import gzip
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent


def load_env(path=ROOT / ".env"):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def pull(endpoint, params, destination, redact=("key",)):
    url = endpoint + "?" + urlencode(params)
    contact = os.environ.get("WIKIPEDIA_CONTACT", "")
    headers = {"User-Agent": f"DAEN328-source-exploration/1.0 ({contact})", "Accept-Encoding": "gzip"}
    requested_at = datetime.now(timezone.utc).isoformat()
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            raw, status = response.read(), response.status
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except HTTPError as e:
        sys.exit(f"HTTP {e.code} from {endpoint}: {e.read()[:300]!r}")
    json.loads(raw)  # validate before saving as evidence
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = destination / f"{stamp}.json"
    path.write_bytes(raw)
    logged = urlencode({k: ("REDACTED" if k in redact else v) for k, v in params.items()})
    path.with_suffix(".request.json").write_text(json.dumps({
        "requested_at_utc": requested_at, "url": endpoint + "?" + logged,
        "http_status": status, "response_file": path.name,
    }, indent=2), encoding="utf-8")
    print(f"HTTP {status}: {path}")


def youtube():
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        sys.exit("Set YOUTUBE_API_KEY in .env")
    pull("https://www.googleapis.com/youtube/v3/videos", {
        "part": "snippet,statistics,contentDetails", "chart": "mostPopular",
        "regionCode": "US", "maxResults": 50, "key": key,
    }, HERE / "youtube" / "samples" / "most_popular")


def wikipedia():
    pull("https://en.wikipedia.org/w/api.php", {
        "action": "query", "list": "recentchanges", "format": "json", "rclimit": 100,
        "rcprop": "title|ids|sizes|flags|user|userid|timestamp|comment|tags|loginfo",
        "rctype": "edit|new|log",
    }, HERE / "wikipedia" / "samples" / "recentchanges")


def stackexchange():
    params = {"site": "stackoverflow", "order": "desc", "sort": "creation", "pagesize": 100}
    if os.environ.get("STACKEXCHANGE_KEY"):
        params["key"] = os.environ["STACKEXCHANGE_KEY"]
    pull("https://api.stackexchange.com/2.3/questions", params,
         HERE / "stackexchange" / "samples" / "questions")


SOURCES = {"youtube": youtube, "wikipedia": wikipedia, "stackexchange": stackexchange}

if __name__ == "__main__":
    load_env()
    for name in sys.argv[1:] or SOURCES:
        SOURCES[name]()
