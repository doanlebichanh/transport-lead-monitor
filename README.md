# Daily transport leads

Python pipeline for permitted public RSS/Atom or HTML transport sources.

It filters for 45RF/48RF reefer and configured route terms, removes duplicate URLs, writes a CSV, optionally appends rows to Google Sheets, and sends a detailed summary plus the CSV to Telegram.

## Configure sources

Edit `sources.json`. Start with RSS/Atom sources:

```json
{
  "sources": [
    {
      "name": "Public transport feed",
      "url": "https://example.com/feed.xml",
      "type": "rss"
    }
  ]
}
```

For a public HTML source, use `type: "html"`. HTML sources are fetched through Crawlbase and may require `item_selector` and `title_selector` matching that site's markup.

Do not add Facebook login cookies, passwords, or private-group URLs. Use only sources and data access that you are authorized to use.

## Local test

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py --config config.json --report transport_leads.csv --no-telegram
```

The first test should use RSS sources. For a real Telegram run, set these environment variables first:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_IDS       # semicolon separated
```

## GitHub Actions secrets

Add repository secrets under `Settings > Secrets and variables > Actions`:

```text
CRAWLBASE_TOKEN
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_IDS
GOOGLE_SERVICE_ACCOUNT_JSON
GOOGLE_SHEET_ID
DATABASE_URL
```

Create an optional repository variable named `GOOGLE_SHEET_WORKSHEET`; its default is `Leads`.

`DATABASE_URL` is optional. If set, the script creates a `transport_leads` PostgreSQL table and inserts new URLs only. A Supabase PostgreSQL connection string can be used.

For Google Sheets, enable the Sheets API, create a service account, share the spreadsheet with the service account email as Editor, and store the service-account JSON as `GOOGLE_SERVICE_ACCOUNT_JSON`.

The workflow reads `sources.json` and runs every hour at minute 00. GitHub cron uses UTC, but this schedule is timezone-independent. GitHub may start scheduled jobs a few minutes late. Use `workflow_dispatch` to test manually.

The default configuration uses public RSS/Atom feeds only. It does not log in to social networks or read private groups. RSS results are public industry/news signals and should be verified manually before contacting anyone.

The configuration also reads the public order data rendered on XECONT.vn. This uses only the public homepage response and does not log in or submit orders. The page may change its format; if that happens, the source is reported as failed while the other feeds continue.

The configuration can also read public Facebook group posts through ScrapeCreators. Set the
`SCRAPECREATORS_API_KEY` GitHub Actions secret and add only public groups that you are
authorized to monitor. The collector uses the group's public URL, follows the API cursor for
a bounded number of pages, and keeps posts only when they contain a request for a vehicle plus
container/cargo or route signals. It does not use Facebook cookies, passwords, private-group
access, automatic comments, or messages. ScrapeCreators currently limits this endpoint to
three posts per request, so the source's `max_pages` controls the bounded number of requests.

## Report fields

The CSV includes collection time, published time, source, title, public contact phones found in the post, source URL, and a short content field. Verify every lead manually before contacting it. Do not use the pipeline for bulk unsolicited messages.

Telegram receives a formatted HTML summary with icons, one block per lead, matching titles, sources, publication times, public phone numbers found, content, and clickable source links. Long summaries are split into several messages to stay within Telegram's message limit; the CSV is also attached.
