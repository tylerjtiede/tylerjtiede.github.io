# rating_calculator.py
#
# Drop-in replacement for the Flask backend.
# Contains all bug fixes and improvements from the desktop app:
#
#   Bug fix 1 — Lookback window anchored to most recent rated round,
#               not the next update deadline. Also implements the
#               24-month fallback when fewer than 8 rounds exist.
#
#   Bug fix 2 — Respects included == 'Yes' so rounds PDGA already
#               dropped as outliers don't get re-counted.
#
#   + Retry logic with backoff on HTTP failures
#   + Graceful error handling on malformed HTML

import re
import time
from datetime import datetime
from operator import itemgetter

import numpy as np
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ONE_YEAR_SECS     = 365 * 24 * 60 * 60
TWO_YEARS_SECS    = 2 * ONE_YEAR_SECS
MIN_ROUNDS_1_YEAR = 8

MAX_RETRIES   = 3
RETRY_BACKOFF = 2  # seconds

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "pdga-ratings-calculator/1.0"})


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _fetch(url: str) -> BeautifulSoup:
    """Fetch url with retries. Raises RuntimeError on repeated failure."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = SESSION.get(url, timeout=15)
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} attempts: {last_exc}")


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def _parse_date(date_str: str) -> int:
    """Parse a PDGA date string to a Unix timestamp."""
    if "Date:" in date_str:
        date_str = date_str.split("Date: ")[1]
    try:
        if "to" in date_str:
            _, end_part = date_str.split("to")
            dt = datetime.strptime(end_part.strip(), "%d-%b-%Y")
        else:
            dt = datetime.strptime(date_str.strip(), "%d-%b-%Y")
        return int(dt.timestamp())
    except Exception as e:
        raise ValueError(f"Could not parse date '{date_str}': {e}")


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def _scrape_current_rating(doc_stats: BeautifulSoup, doc_history: BeautifulSoup) -> int:
    rating_li = doc_stats.find("li", class_="current-rating")
    if rating_li:
        match = re.search(r"Current Rating:(\d+)", rating_li.get_text(strip=True))
        if match:
            return int(match.group(1))
    table = doc_history.find("table", id="player-results-history")
    if table:
        first_row = table.find("tbody").find("tr")
        return int(first_row.find("td", class_="player-rating").get_text(strip=True))
    raise ValueError("Could not find current rating.")


def _scrape_ratings_schedule(doc: BeautifulSoup) -> list:
    table = doc.find("table")
    schedule = []
    for row in table.find("tbody").find_all("tr"):
        cells = row.find_all("td")
        if len(cells) == 2:
            try:
                deadline    = int(datetime.strptime(cells[0].get_text(strip=True), "%B %d, %Y").timestamp())
                publication = int(datetime.strptime(cells[1].get_text(strip=True), "%B %d, %Y").timestamp())
                schedule.append({"deadline": deadline, "publication": publication})
            except ValueError:
                continue
    return schedule


def _scrape_detail_tournaments(doc_detail: BeautifulSoup) -> list:
    tournaments = []
    for row in doc_detail.find_all("tr"):
        tournament_cell = row.find("td", class_="tournament")
        if not tournament_cell:
            continue
        t = {}
        link = tournament_cell.find("a")
        if link:
            t["name"] = link.get_text(strip=True)
            t["link"] = link["href"]
        for key, cls in {
            "tier": "tier", "date": "date", "division": "division",
            "round": "round tooltip", "score": "score", "rating": "round-rating",
            "evaluated": "evaluated", "included": "included",
        }.items():
            cell = row.find("td", class_=cls)
            if cell:
                t[key] = cell.get_text(strip=True)
        try:
            t["rating"]    = int(t["rating"])
            t["timestamp"] = _parse_date(t["date"])
        except (KeyError, ValueError):
            continue
        tournaments.append(t)
    return tournaments


def _scrape_stats_tournaments(doc_stats: BeautifulSoup) -> list:
    tournaments = []
    for row in doc_stats.select("tbody tr"):
        t = {}
        tournament_td = row.find("td", class_="tournament")
        if not tournament_td:
            continue
        for key, cls in [("place","place"),("points","points"),("tier","tier"),("prize","prize")]:
            cell = row.find("td", class_=cls)
            if cell:
                t[key] = cell.get_text(strip=True)
        link_tag = tournament_td.find("a")
        if link_tag:
            t["name"] = link_tag.get_text(strip=True)
            t["link"] = link_tag["href"].split("#")[0]
        dates_cell = row.find("td", class_="dates")
        if dates_cell:
            t["date"] = dates_cell.get_text(strip=True)
            try:
                t["timestamp"] = _parse_date(t["date"])
            except ValueError:
                continue
        tournaments.append(t)
    return tournaments


def _scrape_tournament_rounds(href_link: str, pdga_number: str):
    url = f"https://www.pdga.com{href_link}"
    doc = _fetch(url)
    is_league = bool(doc.body.find_all("h4", string=re.compile(r".*League.*"), recursive=True))
    date_el   = doc.find(class_="tournament-date")
    if not date_el:
        return [], 0, "", is_league
    date_str  = date_el.get_text(strip=True)
    timestamp = _parse_date(date_str)
    ratings = []
    for row in doc.find_all("tr"):
        pdga_td = row.find("td", class_="pdga-number")
        if pdga_td and pdga_td.get_text(strip=True) == pdga_number:
            for cell in row.find_all("td", class_="round-rating"):
                text = cell.get_text(strip=True)
                if text:
                    ratings.append(int(text))
            break
    return ratings, timestamp, date_str, is_league


# ---------------------------------------------------------------------------
# Lookback window (bug fix 1)
# ---------------------------------------------------------------------------

def _compute_lookback_window(rounds: list) -> tuple:
    """
    Anchor to most recent rated round (not the next deadline).
    Extend to 24 months if fewer than 8 rounds exist in the 12-month window.
    """
    most_recent = max(r["timestamp"] for r in rounds)
    last_date   = most_recent - ONE_YEAR_SECS
    if len([r for r in rounds if r["timestamp"] > last_date]) < MIN_ROUNDS_1_YEAR:
        last_date = most_recent - TWO_YEARS_SECS
    return most_recent, last_date


# ---------------------------------------------------------------------------
# Rating math
# ---------------------------------------------------------------------------

def _compute_pdga_rating(ratings: list) -> tuple:
    arr        = np.array(ratings, dtype=float)
    avg        = float(np.mean(arr))
    drop_below = float(np.round(max(avg - 100.0, avg - 2.5 * float(np.std(arr)))))
    filtered   = [r for r in ratings if r >= drop_below]
    doubled    = filtered[: len(filtered) // 4]
    if len(filtered) < MIN_ROUNDS_1_YEAR:
        projected = round(float(np.mean(filtered)))
    else:
        projected = round(float(np.mean(filtered + doubled)))
    return projected, drop_below


# ---------------------------------------------------------------------------
# Public API — called by app.py
# ---------------------------------------------------------------------------

def calculate_rating(pdga_number: str, whatif=None) -> dict:
    """
    Fetch player data, apply all PDGA rating rules, and return a result dict
    compatible with the existing frontend JS:

        pdga_rating, rating_change, drop_below,
        incoming_rounds, outgoing_rounds, outlier_rounds
    """
    base         = f"https://www.pdga.com/player/{pdga_number}"
    doc_stats    = _fetch(base)
    doc_detail   = _fetch(f"{base}/details")
    doc_history  = _fetch(f"{base}/history")
    doc_schedule = _fetch("https://www.pdga.com/faq/ratings/when-updated")

    current_rating    = _scrape_current_rating(doc_stats, doc_history)
    ratings_schedule  = _scrape_ratings_schedule(doc_schedule)
    tournaments       = _scrape_detail_tournaments(doc_detail)
    tournaments_stats = _scrape_stats_tournaments(doc_stats)

    now         = int(datetime.now().timestamp())
    next_update = next((d["deadline"] for d in ratings_schedule if d["deadline"] > now), None)
    if next_update is None:
        raise ValueError("Could not determine next ratings update date.")

    # Resolve unrated tournaments not yet on the detail page
    known_links     = {t["link"] for t in tournaments}
    new_tournaments = []

    for t in [x for x in tournaments_stats if x.get("link") not in known_links]:
        try:
            ratings, timestamp, date_str, _ = _scrape_tournament_rounds(t["link"], pdga_number)
        except Exception:
            continue
        if not ratings:
            continue
        for i, rating in enumerate(ratings):
            new_tournaments.append({**t, "rating": rating, "round": i + 1,
                                    "timestamp": timestamp, "date": date_str})

    # Currently playing / recent events
    for li_class in ["current-events", "recent-events"]:
        li = doc_stats.find("li", class_=li_class)
        if not li:
            continue
        for event in li.find_all("a"):
            try:
                ratings, timestamp, date_str, is_league = _scrape_tournament_rounds(
                    event["href"], pdga_number
                )
            except Exception:
                continue
            if is_league and timestamp >= next_update:
                continue
            for i, rating in enumerate(ratings):
                new_tournaments.append({
                    "name": event.get_text(strip=True), "rating": rating,
                    "timestamp": timestamp, "date": date_str, "round": i + 1,
                })

    # Inject whatif rounds
    if whatif:
        for i, r in enumerate(whatif.split(",")[::-1]):
            entry = {"name": f"Hypothetical Round {i+1}", "rating": int(r),
                     "timestamp": now, "round": i + 1}
            new_tournaments.append(entry)

    # Bug fix 1: anchor lookback to most recent round, extend to 24mo if sparse
    all_candidates = new_tournaments + [t for t in tournaments if t.get("evaluated") == "Yes"]
    _, last_date   = _compute_lookback_window(all_candidates)

    # Bug fix 2: respect included == 'Yes' — don't re-count PDGA-dropped outliers
    used_rounds = new_tournaments + [
        t for t in tournaments
        if t.get("evaluated") == "Yes"
        and t["timestamp"] > last_date
        and t.get("included") == "Yes"
    ]

    sorted_rounds = sorted(used_rounds, key=itemgetter("timestamp"), reverse=True)
    pdga_rating, drop_below = _compute_pdga_rating([r["rating"] for r in sorted_rounds])
    rating_change = pdga_rating - current_rating

    rated_in_window = {
        id(t) for t in tournaments
        if t.get("evaluated") == "Yes" and t["timestamp"] > last_date
    }
    outgoing_rounds = [
        t for t in tournaments
        if t.get("evaluated") == "Yes" and t["timestamp"] <= last_date
    ]
    incoming_rounds = sorted(
        [r for r in used_rounds if id(r) not in rated_in_window],
        key=lambda x: (x.get("timestamp", 0), x.get("round", 0)),
    )
    outlier_rounds = [r for r in used_rounds if r["rating"] < drop_below]

    print(f"USER CHECKED PDGA NUMBER: {pdga_number}")

    return {
        "pdga_rating":     pdga_rating,
        "rating_change":   rating_change,
        "drop_below":      int(drop_below),
        "incoming_rounds": incoming_rounds,
        "outgoing_rounds": outgoing_rounds,
        "outlier_rounds":  outlier_rounds,
    }
