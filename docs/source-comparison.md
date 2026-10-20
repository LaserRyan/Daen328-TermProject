# Steam source exploration

## Candidates

1. Reviews dashboard: compare review volume, positive-review percentage, and
   playtime at review across games and dates.
2. News + reviews dashboard: add announcements and compare review activity before
   and after posts. Timing alone does not establish that news caused a change.

## Assignment criteria

| Requirement | Reviews | News added to reviews |
| --- | --- | --- |
| Free | Public endpoint, no key required | Public news endpoint, no key required |
| Volume | Target tens of thousands of reviews; verify totals for chosen games | Growing news feed supplements the review volume |
| Cleaning | Flatten author objects, convert Unix dates, handle missing optional fields, deduplicate repeated pulls | Also clean HTML/BBCode and normalize feed information |
| Incremental | Cursor pagination; sort by update time; upsert by recommendationid | Page older posts with enddate; deduplicate by appid + gid |
| Related entities | games(appid PK), reviews(recommendationid PK, appid FK) | news(appid + gid composite PK, appid FK) |

Example review columns: `recommendationid` (ID stored as text), `appid` (integer,
added from request context), `voted_up` (boolean), `timestamp_created` (Unix seconds,
converted to UTC timestamp), `review` (text). News has `gid` (ID stored as text),
`title` (text), `date` (Unix seconds), and `contents` (text).

## Collection plan and limitations

Run daily after an initial backfill. The source changes when users create/edit
reviews and feeds publish news; daily is our proposed pull schedule, not a source
update guarantee. Review samples default to English and include all purchase types.
They are not representative of all Steam users.

For production review increments, save a timestamp watermark, request Updated
pages until reaching an overlap window, and upsert unique IDs. Date-range filters
apply to creation time, so they must not be treated as an updated-since filter.
Refresh older reviews periodically if changing votes/playtime matter.

For news backfill, inspect the oldest date and request earlier posts. Test timestamp
ties and use overlapping windows with ID deduplication; timestamp paging is not
proof that every historical post is retrievable. New-post pulls also need overlap;
there is no documented updated-since filter for news edits.

Risk: throttling or network outages. For a full extractor, pace requests, respect
Retry-After, retry with backoff, and save progress. The sample scripts deliberately
make one request each and surface errors rather than running bulk retries.

Actual response files and request metadata are the evidence; documentation alone
does not satisfy Phase 1. Team members, roles, verified volume, and source approval
still need to be filled in before submission.

## Official documentation

Saved evidence: successful HTTP 200 responses collected on October 20, 2026
contain five Dota 2 reviews and five news posts, with companion request metadata
under api-exploration/steam/samples/. The news sample includes one blank author,
four posts without tags, and mixed HTML/BBCode content. These small samples
demonstrate access and cleaning needs; they are not the full project dataset.

- [Reviews](https://partner.steamgames.com/doc/webapi/IUserReviewsService)
- [News](https://partner.steamgames.com/doc/webapi/ISteamNews)
