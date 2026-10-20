# Daen328-TermProject

DAEN 328 team project: evaluating Steam reviews and Steam news as data sources.
Source selection is pending team decision and TA approval.

## Try the sample pulls

Requires Python 3.10+ and internet access. No packages or API keys are needed.
From this repository's folder:

```powershell
python api-exploration/steam/pull_reviews.py --appid 570 --count 5
python api-exploration/steam/pull_news.py --appid 570 --count 5
```

Use `--appid 730` for Counter-Strike 2 or `--appid 620` for Portal 2.
Each request saves the actual response plus a companion `.request.json` with
its URL, UTC request time, and HTTP status under `api-exploration/steam/samples/`.
These are small exploration samples, not a complete ETL pipeline.

Reviews: pass `--cursor "VALUE_FROM_PREVIOUS_RESPONSE"` to inspect another page;
use `--sort updated` to inspect recently edited reviews. News: pass
`--enddate UNIX_TIMESTAMP` to inspect older posts. Use `--help` for options.
If a request fails, the script exits with an error; no mock response is substituted.

See [source comparison](docs/source-comparison.md) for criteria and limitations.
