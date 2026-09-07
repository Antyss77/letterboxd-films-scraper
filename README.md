# Letterboxd Films Scraper

A small Python script that fetches the list of films logged on a **public**
[Letterboxd](https://letterboxd.com) profile, handling pagination
automatically.

## Features

- Fetches every film from a user's `/films/` page, across all pages
- **Output formats**: plain text, JSON, or CSV
- **Optional details** per film: director, release year, short description
  (one extra request per film via `--details`)
- **Sort order**: date added (oldest/newest), release date, name, rating...
- Automatic **retry with exponential backoff** on network errors, rate
  limiting (429), bot-detection blocks (403), and server errors (5xx)
- **Progress bar** (via `tqdm`) while fetching
- Randomized delay ("jitter") between requests, and optional **User-Agent
  rotation**, to look less like a predictable bot
- Uses [`cloudscraper`](https://pypi.org/project/cloudscraper/) to get past
  basic Cloudflare bot-detection that blocks plain `requests`

## Installation

```bash
git clone https://github.com/Antyss77/letterboxd-films-scraper.git
cd letterboxd-films-scraper
python3 -m venv venv
source venv/bin/activate      # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

```bash
python letterboxd_scraper.py <username>
```

### Save to a file

```bash
python letterboxd_scraper.py <username> --out films.txt
python letterboxd_scraper.py <username> --out films.json --format json
python letterboxd_scraper.py <username> --out films.csv --format csv
```

### Fetch director, year and description

```bash
python letterboxd_scraper.py <username> --details --out films.json --format json
```

This makes one extra HTTP request per film, so it's noticeably slower for
large profiles (expect roughly 1 request per second per film, plus jitter).

### Choose a sort order

```bash
python letterboxd_scraper.py <username> --sort added-oldest
```

| Value             | Meaning                                  |
|-------------------|-------------------------------------------|
| `added-oldest`    | Date added to Letterboxd, oldest first (default) |
| `added-newest`    | Date added to Letterboxd, newest first    |
| `release-oldest`  | Release date, oldest first                |
| `release-newest`  | Release date, newest first (site default) |
| `name`            | Alphabetical                              |
| `rating-highest`  | Highest rated first                       |
| `rating-lowest`   | Lowest rated first                        |

### Example

```bash
python letterboxd_scraper.py some_username --out films.txt
```

```
Fetching film list: 3page [00:04,  1.4s/page, films=188]

188 film(s) found for some_username:

Parasite
The Grand Budapest Hotel
Everything Everywhere All at Once
...

Saved to films.txt (txt)
```

### All options

| Flag                  | Description                                                        | Default        |
|------------------------|--------------------------------------------------------------------|----------------|
| `--out FILE`            | Save the results to FILE, in addition to printing them             | —              |
| `--format`              | Output file format: `txt`, `json`, or `csv`                        | `txt`          |
| `--sort`                | Sort order (see table above)                                       | `added-oldest` |
| `--details`             | Fetch director, year and description for each film (slower)        | off            |
| `--delay`               | Average seconds between requests (jittered ±30%)                   | `1.5`          |
| `--max-retries`         | Max retry attempts per request before giving up                    | `4`            |
| `--rotate-user-agent`   | Pick a random User-Agent for this run instead of a fixed one       | off            |

## How it works

### Film list

Letterboxd renders each film in a user's grid as:

```html
<li class="griditem">
  <div class="react-component" data-component-class="LazyPoster"
       data-item-name="Some Film (2024)"
       data-target-link="/film/some-film/" ...>
    ...
  </div>
</li>
```

The script parses each page with [BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/),
reads the `data-item-name` and `data-target-link` attributes for every
`li.griditem`, splits out the year suffix, and follows the `Older`
pagination link (`a.next`) until there isn't one. Different sort orders
are requested via Letterboxd's own URL scheme (e.g.
`/<user>/films/by/date-earliest/`), so no manual re-sorting is needed.

### Film details (`--details`)

Letterboxd film pages embed structured metadata as a
`<script type="application/ld+json">` block. The script reads the
`director` and `datePublished`/`dateCreated` fields from there, and falls
back to the `description`/`og:description` meta tag for the synopsis.

> **Note:** this part of the script was written without the ability to
> test against a live Letterboxd film page in the development
> environment. If `--details` comes back with empty director/description
> fields for every film, Letterboxd's markup has likely changed — please
> open an issue with the `<script type="application/ld+json">` contents
> of one film page so the selectors can be fixed.

### Retries and rate limiting

Every request goes through `fetch_with_retry`, which retries on:
- Connection errors and timeouts
- HTTP 429 (rate limited)
- HTTP 403 (bot-detection block)
- HTTP 5xx (server errors)

...with exponential backoff (`2s, 4s, 8s, ...` plus random jitter) up to
`--max-retries` attempts. Between successful requests, the script also
waits `--delay` seconds (±30% random jitter) to avoid a suspiciously
regular request pattern.

## Limitations

- The target profile **must be public**. Private profiles will return an
  empty page and the script will stop with a "no films found" message.
- Letterboxd's HTML/JSON-LD structure may change over time; if the script
  suddenly stops finding films or details, the CSS selectors in
  `extract_films_from_page` / `enrich_with_details` likely need updating.
- `--details` significantly increases the number of requests (one per
  film instead of one per ~72 films), which means longer run times and a
  higher chance of hitting rate limits on large profiles.
- This project scrapes a website that doesn't offer a fully public,
  general-purpose API. Please use it respectfully: keep a reasonable
  delay between requests, and don't hammer the site.

## Disclaimer

This tool is intended for personal use (e.g. exporting your own watched
films). It is not affiliated with or endorsed by Letterboxd. Scraping is
done against the publicly visible HTML of public pages; please review
Letterboxd's [Terms of Use](https://letterboxd.com/legal/terms-of-use/)
before using this at scale.

## License

[MIT](LICENSE)