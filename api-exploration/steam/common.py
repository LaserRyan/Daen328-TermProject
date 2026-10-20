"""Small standard-library helpers for reproducible Steam sample requests."""
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def pull(endpoint, params, destination):
    url = endpoint + "?" + urlencode(params)
    requested_at = datetime.now(timezone.utc).isoformat()
    request = Request(url, headers={"User-Agent": "DAEN328-source-exploration/1.0"})
    with urlopen(request, timeout=30) as response:
        raw = response.read()
        status = response.status
    payload = json.loads(raw)
    # Validate JSON before saving it as successful evidence.
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = destination / f"{stamp}.json"
    path.write_bytes(raw)
    path.with_suffix(".request.json").write_text(json.dumps({
        "requested_at_utc": requested_at, "url": url, "http_status": status,
        "response_file": path.name,
    }, indent=2), encoding="utf-8")
    print(f"HTTP {status}: {path}")
    return payload


SAMPLES = Path(__file__).resolve().parent / "samples"
