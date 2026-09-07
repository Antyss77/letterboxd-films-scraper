#!/usr/bin/env python3
"""
letterboxd_scraper.py

Fetch the list of films logged on a public Letterboxd profile.

Given a Letterboxd username, this script scrapes the user's public
"Films" page (https://letterboxd.com/<username>/films/), follows the
pagination automatically, and prints (or saves) the list of films.

Usage:
    python letterboxd_scraper.py <username>
    python letterboxd_scraper.py <username> --out films.json --format json
    python letterboxd_scraper.py <username> --out films.csv --format csv --details

Example:
    python letterboxd_scraper.py some_username --out films.txt

Requirements:
    pip install -r requirements.txt

Notes:
    - The target profile must be public.
    - This script uses `cloudscraper` instead of plain `requests` to get
      past Letterboxd/Cloudflare's basic bot detection.
    - `--details` fetches each film's own page to add its director, year
      and a short description. This means one extra HTTP request per
      film, so it's opt-in and noticeably slower for large profiles.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from typing import List, Optional

import cloudscraper
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

BASE_URL = "https://letterboxd.com"

# A small pool of realistic desktop User-Agent strings. One is picked at
# random per run when --rotate-user-agent is passed, so repeated runs
# don't always look identical to Cloudflare. We deliberately do *not*
# rotate mid-run: switching User-Agent between requests of the same
# session is itself a suspicious pattern.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

# Letterboxd exposes several sort orders as URL path segments, e.g.
# https://letterboxd.com/<user>/films/by/date-earliest/
# The default (empty path) sorts by release date, newest first.
SORT_PATHS = {
    "added-oldest": "by/date-earliest/",
    "added-newest": "by/date/",
    "release-oldest": "by/release-earliest/",
    "release-newest": "",  # site default
    "name": "by/name/",
    "rating-highest": "by/rating/",
    "rating-lowest": "by/rating-lowest/",
}

YEAR_SUFFIX_RE = re.compile(r"\((\d{4})\)\s*$")

# Letterboxd wraps its JSON-LD payload in CDATA-style comments:
#   /* <![CDATA[ */ {...json...} /* ]]> */
# These markers must be stripped before the content is valid JSON.
LD_JSON_CDATA_RE = re.compile(r"/\*\s*<!\[CDATA\[\s*\*/|/\*\s*\]\]>\s*\*/")


@dataclass
class Film:
    """A single film entry, optionally enriched with extra details."""

    title: str
    year: Optional[str] = None
    director: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None


class FetchError(Exception):
    """Raised when a page could not be fetched after all retry attempts."""


# --------------------------------------------------------------------------
# Networking: session setup, retries, backoff
# --------------------------------------------------------------------------


def create_session(rotate_user_agent: bool = False) -> cloudscraper.CloudScraper:
    """Build a cloudscraper session configured to look like a real browser.

    Args:
        rotate_user_agent: If True, pick a random User-Agent from
            USER_AGENTS for this run instead of always using the same one.

    Returns:
        A ready-to-use cloudscraper session.
    """
    session = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    user_agent = random.choice(USER_AGENTS) if rotate_user_agent else USER_AGENTS[0]
    session.headers.update(
        {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": user_agent,
        }
    )
    return session


def fetch_with_retry(
    session: cloudscraper.CloudScraper,
    url: str,
    *,
    referer: Optional[str] = None,
    max_retries: int = 4,
    base_backoff: float = 2.0,
    timeout: float = 20.0,
) -> Optional[BeautifulSoup]:
    """Fetch a URL, retrying on transient errors with exponential backoff.

    Retries on network errors (connection/timeout), rate limiting (429),
    bot-detection blocks (403), and server errors (5xx). Gives up
    immediately on other client errors (e.g. 400, 401).

    Args:
        session: The cloudscraper session to use.
        url: The page to fetch.
        referer: Optional Referer header (the previously visited page).
        max_retries: Maximum number of attempts before giving up.
        base_backoff: Base delay in seconds; doubles on each retry, plus
            a small random jitter.
        timeout: Per-request timeout, in seconds.

    Returns:
        Parsed HTML, or None if the page doesn't exist (404).

    Raises:
        FetchError: If every retry attempt fails.
    """
    headers = {"Referer": referer} if referer else {}
    last_exc: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            response = session.get(url, headers=headers, timeout=timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            wait = base_backoff * (2 ** (attempt - 1)) + random.uniform(0, 1)
            print(
                f"  Network error ({exc.__class__.__name__}) on attempt "
                f"{attempt}/{max_retries}. Retrying in {wait:.1f}s...",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        if response.status_code == 404:
            return None

        if response.status_code == 429 or response.status_code == 403 or response.status_code >= 500:
            last_exc = requests.exceptions.HTTPError(
                f"{response.status_code} for {url}", response=response
            )
            wait = base_backoff * (2 ** (attempt - 1)) + random.uniform(0, 1)
            print(
                f"  Blocked or server error ({response.status_code}) on attempt "
                f"{attempt}/{max_retries}. Retrying in {wait:.1f}s...",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            raise FetchError(f"Failed to fetch {url}: {exc}") from exc

        return BeautifulSoup(response.text, "html.parser")

    raise FetchError(f"Failed to fetch {url} after {max_retries} attempts") from last_exc


def jitter_sleep(delay: float) -> None:
    """Sleep for `delay` seconds, +/- 30% random jitter.

    A little randomness makes request timing look more human than a
    perfectly fixed interval.
    """
    if delay <= 0:
        return
    time.sleep(random.uniform(delay * 0.7, delay * 1.3))


# --------------------------------------------------------------------------
# Film list parsing
# --------------------------------------------------------------------------


def build_films_url(username: str, sort: str, page: int) -> str:
    """Build the URL for a given page of a user's film list.

    Args:
        username: Letterboxd username.
        sort: One of SORT_PATHS' keys.
        page: 1-indexed page number.
    """
    sort_path = SORT_PATHS.get(sort, "")
    path = f"{username}/films/"
    if sort_path:
        path += sort_path
    if page > 1:
        path += f"page/{page}/"
    return f"{BASE_URL}/{path}"


def extract_films_from_page(soup: BeautifulSoup) -> List[Film]:
    """Extract Film entries from one page of a user's film grid.

    Letterboxd renders each film as:
        <li class="griditem">
          <div class="react-component" data-component-class="LazyPoster"
               data-item-name="Some Film (2024)"
               data-target-link="/film/some-film/" ...>

    Args:
        soup: Parsed HTML of a films grid page.

    Returns:
        A list of Film objects (title, year, url), in page order.
    """
    films: List[Film] = []

    for item in soup.select("li.griditem"):
        poster = item.select_one("div.react-component[data-item-name]")
        raw_title = poster.get("data-item-name") if poster else None
        link = poster.get("data-target-link") if poster else None

        if not raw_title:
            # Fallback: the image's alt text also contains the title.
            img = item.select_one("img")
            raw_title = img.get("alt") if img else None

        if not raw_title:
            continue

        year_match = YEAR_SUFFIX_RE.search(raw_title)
        year = year_match.group(1) if year_match else None
        title = YEAR_SUFFIX_RE.sub("", raw_title).strip()
        url = f"{BASE_URL}{link}" if link else None

        films.append(Film(title=title, year=year, url=url))

    return films


def has_next_page(soup: BeautifulSoup) -> bool:
    """Check whether the page's pagination has an "Older" / next link."""
    return soup.select_one("a.next") is not None


def get_all_films(
    username: str,
    session: cloudscraper.CloudScraper,
    *,
    sort: str = "added-oldest",
    delay: float = 1.5,
    max_retries: int = 4,
) -> List[Film]:
    """Fetch every film from a user's public Letterboxd film list.

    Iterates through pages until there is no next page, the profile has
    no (more) films, or fetching fails after all retries (in which case
    whatever was collected so far is returned).

    Args:
        username: Letterboxd username (case-insensitive).
        session: The cloudscraper session to use.
        sort: Sort order; see SORT_PATHS for available values.
        delay: Average seconds to wait between page requests (jittered).
        max_retries: Passed through to fetch_with_retry.

    Returns:
        A list of Film objects across all pages, in the chosen sort order.
    """
    all_films: List[Film] = []
    previous_url: Optional[str] = None
    page = 1

    with tqdm(desc="Fetching film list", unit="page") as pbar:
        while True:
            url = build_films_url(username, sort, page)

            try:
                soup = fetch_with_retry(
                    session, url, referer=previous_url, max_retries=max_retries
                )
            except FetchError as exc:
                print(
                    f"Warning: {exc}. Keeping the {len(all_films)} film(s) "
                    f"collected so far.",
                    file=sys.stderr,
                )
                break

            if soup is None:
                break  # 404: no more pages.

            films = extract_films_from_page(soup)
            if not films:
                break  # Empty page: end of list, or private/invalid profile.

            all_films.extend(films)
            previous_url = url
            pbar.update(1)
            pbar.set_postfix(films=len(all_films))

            if not has_next_page(soup):
                break

            page += 1
            jitter_sleep(delay)

    return all_films


# --------------------------------------------------------------------------
# Per-film detail enrichment (director, description)
# --------------------------------------------------------------------------


def enrich_with_details(
    films: List[Film],
    session: cloudscraper.CloudScraper,
    *,
    delay: float = 1.0,
    max_retries: int = 4,
) -> None:
    """Fetch each film's own page to fill in director and description.

    Films are updated in place. Letterboxd film pages embed a JSON-LD
    <script type="application/ld+json"> block with structured metadata;
    this is used as the primary source, with the `og:description` /
    `description` meta tag as a fallback for the synopsis.

    NOTE: this relies on Letterboxd's current page markup, which was not
    verified against a live request while writing this script (no
    network access to letterboxd.com in this environment). If director
    or description come back empty for every film, the site's JSON-LD
    structure has likely changed -- please open an issue with a sample
    film page's <script type="application/ld+json"> block.

    Args:
        films: The films to enrich (mutated in place).
        session: The cloudscraper session to use.
        delay: Average seconds to wait between requests (jittered).
        max_retries: Passed through to fetch_with_retry.
    """
    for film in tqdm(films, desc="Fetching film details", unit="film"):
        if not film.url:
            continue

        try:
            soup = fetch_with_retry(session, film.url, max_retries=max_retries)
        except FetchError as exc:
            print(f"  Warning: could not fetch details for '{film.title}': {exc}", file=sys.stderr)
            continue

        if soup is None:
            continue

        ld_json_tag = soup.find("script", type="application/ld+json")
        if ld_json_tag and ld_json_tag.string:
            try:
                # Letterboxd wraps its JSON-LD in CDATA-style comments, e.g.:
                #   /* <![CDATA[ */ {...} /* ]]> */
                # which isn't valid JSON on its own -- strip those markers
                # before parsing.
                raw = LD_JSON_CDATA_RE.sub("", ld_json_tag.string).strip()
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = None

            if isinstance(data, dict):
                director_field = data.get("director")
                if isinstance(director_field, list):
                    names = [d.get("name") for d in director_field if isinstance(d, dict) and d.get("name")]
                    if names:
                        film.director = ", ".join(names)
                elif isinstance(director_field, dict) and director_field.get("name"):
                    film.director = director_field["name"]

                if not film.year:
                    date_str = data.get("datePublished") or data.get("dateCreated")
                    if date_str:
                        film.year = str(date_str)[:4]

        if not film.description:
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if not meta_desc or not meta_desc.get("content"):
                meta_desc = soup.find("meta", attrs={"property": "og:description"})
            if meta_desc and meta_desc.get("content"):
                film.description = meta_desc["content"].strip()

        jitter_sleep(delay)


# --------------------------------------------------------------------------
# Output formatting
# --------------------------------------------------------------------------


def format_line(film: Film, with_details: bool) -> str:
    """Render a single film as a human-readable line for stdout/txt output."""
    line = film.title
    if with_details and film.year:
        line += f" ({film.year})"
    if with_details and film.director:
        line += f" — dir. {film.director}"
    return line


def write_txt(films: List[Film], path: str, with_details: bool) -> None:
    lines = [format_line(film, with_details) for film in films]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_json(films: List[Film], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(film) for film in films], f, ensure_ascii=False, indent=2)


def write_csv(films: List[Film], path: str) -> None:
    fieldnames = ["title", "year", "director", "description", "url"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for film in films:
            writer.writerow(asdict(film))


def save_films(films: List[Film], path: str, fmt: str, with_details: bool) -> None:
    if fmt == "json":
        write_json(films, path)
    elif fmt == "csv":
        write_csv(films, path)
    else:
        write_txt(films, path, with_details)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch the list of films logged on a public Letterboxd profile."
    )
    parser.add_argument("username", help="Letterboxd username, e.g. some_username")
    parser.add_argument(
        "--out",
        metavar="FILE",
        help="Save the results to FILE, in addition to printing them",
    )
    parser.add_argument(
        "--format",
        choices=["txt", "json", "csv"],
        default="txt",
        help="Output file format when --out is used (default: txt)",
    )
    parser.add_argument(
        "--sort",
        choices=sorted(SORT_PATHS.keys()),
        default="added-oldest",
        help="Sort order for the film list (default: added-oldest)",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Fetch each film's director, year and a short description "
        "(1 extra HTTP request per film -- slower on large profiles)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="Average seconds to wait between requests, jittered +/-30%% (default: 1.5)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=4,
        help="Max retry attempts per request before giving up (default: 4)",
    )
    parser.add_argument(
        "--rotate-user-agent",
        action="store_true",
        help="Pick a random User-Agent for this run instead of a fixed one",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    session = create_session(rotate_user_agent=args.rotate_user_agent)

    films: List[Film] = []
    try:
        films = get_all_films(
            args.username,
            session,
            sort=args.sort,
            delay=args.delay,
            max_retries=args.max_retries,
        )

        if not films:
            print(
                "No films found. Check the username, and make sure the profile is public.",
                file=sys.stderr,
            )
            sys.exit(1)

        if args.details:
            enrich_with_details(films, session, delay=args.delay, max_retries=args.max_retries)

    except KeyboardInterrupt:
        print(
            f"\n\nInterrupted by user. Keeping the {len(films)} film(s) collected so far.",
            file=sys.stderr,
        )
        if not films:
            sys.exit(130)  # Standard exit code for SIGINT.

    print(f"\n{len(films)} film(s) found for {args.username}:\n")
    for film in films:
        print(format_line(film, args.details))

    if args.out:
        save_films(films, args.out, args.format, args.details)
        print(f"\nSaved to {args.out} ({args.format})", file=sys.stderr)


if __name__ == "__main__":
    main()