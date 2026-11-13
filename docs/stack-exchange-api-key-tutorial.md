# How to Get a Stack Exchange API Key

A step-by-step guide to registering an app on Stack Apps, getting your API key, and using it to raise your daily request quota from **300 to 10,000**.

---

## What You're Getting (and Why)

The Stack Exchange API (`api.stackexchange.com`) works without any key, but anonymous requests are capped at **300 requests per day per IP address**. Registering a free "app" on [Stack Apps](https://stackapps.com) gives you a **key** that raises that cap to **10,000 requests per day**.

When you register, you receive three values:

| Value | What it's for | Keep it secret? |
|---|---|---|
| **Key** | Raises your quota. This is all you need for reading public data. | No. It's safe in URLs and client-side code. |
| **Client Id** | Used only for OAuth (logging users in). | Not sensitive, but not needed for data analysis. |
| **Client Secret** | Used only for server-side OAuth flows. | **Yes. Never share or commit it.** |

For pulling questions, answers, tags, users, and other public data, **you only need the Key**.

---

## Step 1: Create or Log Into a Stack Exchange Account

1. Go to [https://stackapps.com](https://stackapps.com).
2. Click **Log in** in the top-right corner.
3. Sign in with your existing Stack Overflow / Stack Exchange account (Google, GitHub, or email login all work).
   - If you don't have an account, click **Sign up** and create one. It's free.
4. If this is your first time on Stack Apps, you may be asked to confirm joining the site. Accept.

> Your account is shared across all Stack Exchange sites, so if you already use Stack Overflow, you're set.

---

## Step 2: Open the App Registration Page

Go directly to:

**[https://stackapps.com/apps/oauth/register](https://stackapps.com/apps/oauth/register)**

You'll see a form titled something like **"Register Your V2.0 Application."**

---

## Step 3: Fill Out the Registration Form

Fill in the required fields. Optional fields can be left blank.

| Field | What to enter | Example |
|---|---|---|
| **Application Name** | Any descriptive name. | `Tag Trends Class Project` |
| **Description** | A sentence or two about what you're doing. | `Analyzing question volume and tag trends on Stack Overflow for a data visualization project.` |
| **OAuth Domain** | A domain you control. If you aren't building a login flow, use a placeholder. | `localhost` or `stackexchange.com` |
| **Application Website** | Your project's URL, GitHub repo, or a placeholder. | `https://github.com/yourname/your-repo` |
| **Application Icon** *(optional)* | Leave blank. | |
| **Enable Client Side OAuth Flow** *(checkbox)* | Leave unchecked unless you're building a browser login. | ☐ |
| **Disable Desktop Application OAuth Redirect Uri** *(checkbox, if shown)* | Leave unchecked. | ☐ |

Then click **Register Your Application**.

---

## Step 4: Copy Your Key

After registering, you'll land on your app's settings page, which shows:

- **Client Id**: a number, e.g. `12345`
- **Client Secret**: a long string (**keep private**)
- **Key**: a string ending in `((`, e.g. `AbCdEf1234567890((`

**Copy the Key** and save it somewhere safe, such as a password manager or a local `.env` file.

> **Can't find it later?** Go to [https://stackapps.com/apps/oauth](https://stackapps.com/apps/oauth) while logged in to see all your registered apps and their keys.

---

## Step 5: Use the Key in Your Requests

Add `&key=YOUR_KEY` to any API URL.

### In the browser

```
https://api.stackexchange.com/2.3/info?site=stackoverflow&key=YOUR_KEY
```

### In Python

```python
import os
import requests

API_KEY = os.environ["SE_API_KEY"]  # store it in an environment variable

resp = requests.get(
    "https://api.stackexchange.com/2.3/info",
    params={"site": "stackoverflow", "key": API_KEY},
)
data = resp.json()

print("Quota max:", data["quota_max"])            # should be 10000
print("Quota remaining:", data["quota_remaining"])
```

Set the environment variable before running:

```bash
# macOS / Linux
export SE_API_KEY="YourKeyHere(("

# Windows (PowerShell)
$env:SE_API_KEY = "YourKeyHere(("
```

---

## Step 6: Verify It Worked

Check the `quota_max` field in any API response:

- `quota_max: 300` → the key **isn't** being sent. Check your URL or parameters.
- `quota_max: 10000` → the key is working.

---

## ⚠️ Reminders

### The Key is not a secret; the Client Secret is

The Key is designed to be visible (in URLs, front-end JavaScript, etc.). The **Client Secret** must never be shared, posted, or committed to Git. Add `.env` to your `.gitignore` anyway as a good habit.

### Quota is per key + IP address

The 10,000/day limit applies to each **key and IP address pair**, not to the key alone.

- **One key can be shared by a whole group.** Each member running requests from a *different* network gets their own 10,000/day.
- **Registering more keys on the same IP doesn't help you much.** Your traffic still comes from one address and is subject to per-IP limits.
- **Quotas reset daily** (at midnight UTC).

### Check whether your group shares an IP

Campus, office, and dorm networks often route everyone through the **same public IP**. If your group is on the same Wi-Fi, you may be sharing limits without realizing it.

- Have each member visit a "what's my IP" site.
- If the IPs match, split the work across different networks (home internet, phone hotspots).

### Respect the `backoff` field

Some responses include a `backoff` value (in seconds). When you see it, **wait that many seconds before calling that same method again.** Ignoring it can get your IP temporarily blocked.

```python
import time

data = resp.json()
if "backoff" in data:
    time.sleep(data["backoff"])
```

### Don't flood the API

- Stack Exchange caps simultaneous in-flight requests per IP (historically about 30). Keep requests sequential or lightly parallel.
- Responses are cached heavily, so don't send the **same** request more than about once a minute.
- Don't try to get around limits with many keys, proxies, or rotating IPs. That goes against the API's terms and can get your key or IP banned.

### Watch your quota as you go

Every response includes `quota_remaining`. Log it, and stop or slow down before you hit zero.

```python
if data["quota_remaining"] < 100:
    print("Running low on quota, stopping for today.")
```

### Use `filter=total` to count cheaply

If you only need **how many** items match a query (e.g. questions tagged `python` in March 2024), add `filter=total`. You get a single count back in **one request** instead of paging through thousands of results.

```
https://api.stackexchange.com/2.3/questions?site=stackoverflow&tagged=python&fromdate=1709251200&todate=1711929600&filter=total&key=YOUR_KEY
```

(`fromdate` / `todate` are Unix timestamps.)

### Responses are gzip-compressed

The API always gzips responses. Libraries like Python's `requests` handle this automatically, but raw `curl` needs `--compressed`.

```bash
curl --compressed "https://api.stackexchange.com/2.3/info?site=stackoverflow&key=YOUR_KEY"
```

### Max 100 items per page

Use `pagesize=100` and the `page` parameter to paginate. Check the `has_more` field to know when to stop.

### Consider alternatives for very large pulls

If you need *every* question or full-text analysis, the API may be the slow route:

- **[Stack Exchange Data Explorer (SEDE)](https://data.stackexchange.com)**: run SQL queries in the browser against a weekly-refreshed copy of the data. No quota.
- **Stack Exchange data dump**: full downloadable archives of site content.

---

## Quick Checklist

- [ ] Logged into [stackapps.com](https://stackapps.com)
- [ ] Registered an app at [stackapps.com/apps/oauth/register](https://stackapps.com/apps/oauth/register)
- [ ] Copied the **Key** (not the Client Secret)
- [ ] Stored it in an environment variable or `.env` file
- [ ] Confirmed `quota_max` is `10000`
- [ ] Group members checked they're on different IPs
- [ ] Code handles `backoff` and watches `quota_remaining`

---

## Useful Links

- API documentation: [https://api.stackexchange.com/docs](https://api.stackexchange.com/docs)
- Throttling rules: [https://api.stackexchange.com/docs/throttle](https://api.stackexchange.com/docs/throttle)
- Register an app: [https://stackapps.com/apps/oauth/register](https://stackapps.com/apps/oauth/register)
- Your registered apps: [https://stackapps.com/apps/oauth](https://stackapps.com/apps/oauth)
- Data Explorer: [https://data.stackexchange.com](https://data.stackexchange.com)
