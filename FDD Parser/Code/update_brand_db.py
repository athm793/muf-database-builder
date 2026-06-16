#!/usr/bin/env python3
"""
UPDATE BRAND DATABASE
=====================
Builds and maintains brand_db.json — the catalogue of US franchise brands
used by the "Search by category" NL query feature.

Two phases:
  1. Seed (~230 major brands, hardcoded, instant)
  2. WI DFI supplement — Playwright scrapes the full Wisconsin registrant list
     and adds any brands not already in the seed.  Run with --full to enable.
     New brands without tags get categorised by Claude Haiku (cached).

Usage:
    py update_brand_db.py            # rebuild from seed only
    py update_brand_db.py --full     # seed + WI DFI live scrape
    py update_brand_db.py --debug    # --full with screenshots saved

Output:
    Code/brand_db.json
"""

import re, json, sys, time, asyncio, subprocess, argparse
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

HERE       = Path(__file__).resolve().parent
DB_FILE    = HERE / "brand_db.json"
CAT_CACHE  = HERE / "cache" / "brand_categories.json"
DEBUG_DIR  = HERE / "debug_brands"

CLAUDE_CMD = "claude.cmd" if sys.platform == "win32" else "claude"
MODEL      = "haiku"
CLI_TIMEOUT = 60

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0 Safari/537.36")


def log(msg=""):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}" if msg else "", flush=True)


# ── SEED LIST ─────────────────────────────────────────────────────────────────
# (name, [category_tags])  — tags are used by Claude to match NL queries.

SEED: list[tuple[str, list[str]]] = [
    # QSR Burger
    ("McDonald's",              ["qsr", "burger"]),
    ("Burger King",             ["qsr", "burger"]),
    ("Wendy's",                 ["qsr", "burger"]),
    ("Five Guys",               ["qsr", "burger"]),
    ("Shake Shack",             ["qsr", "burger", "fast_casual"]),
    ("Whataburger",             ["qsr", "burger"]),
    ("Jack in the Box",         ["qsr", "burger"]),
    ("Sonic Drive-In",          ["qsr", "burger"]),
    ("Culver's",                ["qsr", "burger"]),
    ("Rally's",                 ["qsr", "burger"]),
    ("Checkers",                ["qsr", "burger"]),
    ("Hardee's",                ["qsr", "burger"]),
    ("Carl's Jr.",              ["qsr", "burger"]),
    ("Steak 'n Shake",          ["qsr", "burger"]),
    ("Fatburger",               ["qsr", "burger"]),
    ("Smashburger",             ["qsr", "burger", "fast_casual"]),
    ("Habit Burger",            ["qsr", "burger", "fast_casual"]),
    ("Freddy's",                ["qsr", "burger"]),
    ("Wayback Burgers",         ["qsr", "burger"]),
    ("BurgerFi",                ["qsr", "burger", "fast_casual"]),

    # QSR Chicken
    ("KFC",                     ["qsr", "chicken"]),
    ("Chick-fil-A",             ["qsr", "chicken"]),
    ("Popeyes",                 ["qsr", "chicken"]),
    ("Wingstop",                ["qsr", "chicken", "wings"]),
    ("Raising Cane's",          ["qsr", "chicken"]),
    ("Zaxby's",                 ["qsr", "chicken", "fast_casual"]),
    ("El Pollo Loco",           ["qsr", "chicken"]),
    ("Church's Chicken",        ["qsr", "chicken"]),
    ("Bojangles",               ["qsr", "chicken"]),
    ("PDQ",                     ["qsr", "chicken", "fast_casual"]),
    ("Buffalo Wild Wings",      ["qsr", "chicken", "wings", "casual_dining"]),
    ("Wing Zone",               ["qsr", "chicken", "wings"]),
    ("Hooters",                 ["qsr", "chicken", "wings", "casual_dining"]),
    ("Dave's Hot Chicken",      ["qsr", "chicken", "fast_casual"]),
    ("Slim Chickens",           ["qsr", "chicken", "fast_casual"]),
    ("Huey Magoo's",            ["qsr", "chicken", "fast_casual"]),
    ("Golden Corral",           ["casual_dining", "buffet"]),

    # QSR Pizza
    ("Domino's",                ["qsr", "pizza"]),
    ("Pizza Hut",               ["qsr", "pizza"]),
    ("Papa John's",             ["qsr", "pizza"]),
    ("Little Caesars",          ["qsr", "pizza"]),
    ("Marco's Pizza",           ["qsr", "pizza"]),
    ("Jet's Pizza",             ["qsr", "pizza"]),
    ("Round Table Pizza",       ["qsr", "pizza"]),
    ("Cicis",                   ["qsr", "pizza", "buffet"]),
    ("Godfather's Pizza",       ["qsr", "pizza"]),
    ("Hungry Howie's",          ["qsr", "pizza"]),
    ("Sbarro",                  ["qsr", "pizza"]),
    ("MOD Pizza",               ["qsr", "pizza", "fast_casual"]),
    ("Blaze Pizza",             ["qsr", "pizza", "fast_casual"]),

    # QSR Mexican
    ("Taco Bell",               ["qsr", "mexican"]),
    ("Chipotle",                ["qsr", "mexican", "fast_casual"]),
    ("Qdoba",                   ["qsr", "mexican", "fast_casual"]),
    ("Del Taco",                ["qsr", "mexican"]),
    ("Taco John's",             ["qsr", "mexican"]),
    ("Moe's Southwest Grill",   ["qsr", "mexican", "fast_casual"]),
    ("Fuzzy's Taco Shop",       ["qsr", "mexican", "fast_casual"]),
    ("Taco Cabana",             ["qsr", "mexican"]),
    ("Chronic Tacos",           ["qsr", "mexican", "fast_casual"]),
    ("Tijuana Flats",           ["qsr", "mexican", "fast_casual"]),

    # QSR Sandwich
    ("Subway",                  ["qsr", "sandwich"]),
    ("Jersey Mike's",           ["qsr", "sandwich"]),
    ("Jimmy John's",            ["qsr", "sandwich"]),
    ("Firehouse Subs",          ["qsr", "sandwich"]),
    ("Which Wich",              ["qsr", "sandwich"]),
    ("McAlister's Deli",        ["qsr", "sandwich", "fast_casual"]),
    ("Schlotzsky's",            ["qsr", "sandwich", "fast_casual"]),
    ("Potbelly",                ["qsr", "sandwich", "fast_casual"]),
    ("Quiznos",                 ["qsr", "sandwich"]),
    ("Jason's Deli",            ["qsr", "sandwich", "fast_casual"]),
    ("Arby's",                  ["qsr", "sandwich"]),
    ("Charley's Grilled Subs",  ["qsr", "sandwich"]),

    # Coffee / Bakery
    ("Starbucks",               ["coffee", "bakery"]),
    ("Dunkin'",                 ["coffee", "bakery", "qsr"]),
    ("Tim Hortons",             ["coffee", "bakery", "qsr"]),
    ("Panera Bread",            ["bakery", "sandwich", "fast_casual"]),
    ("Einstein Bros.",          ["bakery", "coffee", "fast_casual"]),
    ("Cinnabon",                ["bakery", "dessert"]),
    ("Great Harvest",           ["bakery"]),
    ("Paris Baguette",          ["bakery", "coffee"]),
    ("Caribou Coffee",          ["coffee"]),
    ("Dutch Bros.",             ["coffee"]),
    ("The Human Bean",          ["coffee"]),
    ("Scooter's Coffee",        ["coffee"]),
    ("Ziggi's Coffee",          ["coffee"]),
    ("Biggby Coffee",           ["coffee"]),
    ("7 Brew Coffee",           ["coffee"]),

    # Ice Cream / Dessert
    ("Dairy Queen",             ["qsr", "ice_cream", "dessert"]),
    ("Baskin-Robbins",          ["ice_cream", "dessert"]),
    ("Cold Stone Creamery",     ["ice_cream", "dessert"]),
    ("Yogurtland",              ["ice_cream", "dessert"]),
    ("Rita's Italian Ice",      ["ice_cream", "dessert"]),
    ("Marble Slab Creamery",    ["ice_cream", "dessert"]),
    ("Handel's Homemade",       ["ice_cream", "dessert"]),
    ("Bruster's",               ["ice_cream", "dessert"]),
    ("Andy's Frozen Custard",   ["ice_cream", "dessert"]),
    ("Dippin' Dots",            ["ice_cream", "dessert"]),
    ("Auntie Anne's",           ["bakery", "pretzel"]),
    ("Wetzel's Pretzels",       ["bakery", "pretzel"]),
    ("Jamba",                   ["smoothie", "healthy", "qsr"]),
    ("Smoothie King",           ["smoothie", "healthy"]),

    # Casual Dining
    ("Applebee's",              ["casual_dining"]),
    ("Chili's",                 ["casual_dining"]),
    ("Denny's",                 ["casual_dining", "breakfast"]),
    ("IHOP",                    ["casual_dining", "breakfast"]),
    ("TGI Fridays",             ["casual_dining"]),
    ("Ruby Tuesday",            ["casual_dining"]),
    ("Bob Evans",               ["casual_dining", "breakfast"]),
    ("Perkins",                 ["casual_dining", "breakfast"]),
    ("Cracker Barrel",          ["casual_dining", "breakfast"]),
    ("Friendly's",              ["casual_dining", "ice_cream"]),
    ("Black Bear Diner",        ["casual_dining"]),
    ("Sizzler",                 ["casual_dining"]),
    ("Steak 'n Shake",          ["casual_dining", "burger"]),
    ("Village Inn",             ["casual_dining", "breakfast"]),
    ("Dine Brands",             ["casual_dining"]),

    # Fast Casual Other
    ("Noodles & Company",       ["fast_casual", "asian", "pasta"]),
    ("Portillo's",              ["fast_casual", "burger"]),
    ("Sweetgreen",              ["fast_casual", "salad", "healthy"]),
    ("Panda Express",           ["qsr", "asian"]),
    ("Teriyaki Madness",        ["fast_casual", "asian"]),
    ("P.F. Chang's",            ["casual_dining", "asian"]),
    ("Waba Grill",              ["fast_casual", "asian", "healthy"]),
    ("Lenny's Sub Shop",        ["qsr", "sandwich"]),
    ("WaBa Grill",              ["fast_casual", "asian"]),
    ("Tropical Smoothie Cafe",  ["smoothie", "fast_casual", "healthy"]),

    # Seafood / Other QSR
    ("Long John Silver's",      ["qsr", "seafood"]),
    ("Captain D's",             ["qsr", "seafood"]),
    ("A&W",                     ["qsr", "burger"]),
    ("Hot Dog on a Stick",      ["qsr"]),
    ("Wienerschnitzel",         ["qsr"]),
    ("Nathan's Famous",         ["qsr"]),

    # Fitness
    ("Planet Fitness",          ["fitness", "gym"]),
    ("Anytime Fitness",         ["fitness", "gym"]),
    ("Gold's Gym",              ["fitness", "gym"]),
    ("Orangetheory",            ["fitness", "boutique"]),
    ("F45 Training",            ["fitness", "boutique"]),
    ("Pure Barre",              ["fitness", "boutique"]),
    ("Club Pilates",            ["fitness", "boutique"]),
    ("Snap Fitness",            ["fitness", "gym"]),
    ("Crunch Fitness",          ["fitness", "gym"]),
    ("9Round",                  ["fitness", "boutique"]),
    ("The Bar Method",          ["fitness", "boutique"]),
    ("CycleBar",                ["fitness", "boutique"]),
    ("StretchLab",              ["fitness", "wellness"]),
    ("Body Fit Training",       ["fitness", "boutique"]),
    ("Retro Fitness",           ["fitness", "gym"]),

    # Health / Wellness
    ("The Joint Chiropractic",  ["health", "chiropractic"]),
    ("Massage Envy",            ["health", "wellness", "massage"]),
    ("Hand and Stone",          ["health", "wellness", "massage"]),
    ("Elements Massage",        ["health", "wellness", "massage"]),
    ("ATC Healthcare",          ["health", "staffing"]),

    # Hair / Beauty
    ("Great Clips",             ["beauty", "hair"]),
    ("Sport Clips",             ["beauty", "hair"]),
    ("Supercuts",               ["beauty", "hair"]),
    ("Hair Cuttery",            ["beauty", "hair"]),
    ("Fantastic Sams",          ["beauty", "hair"]),
    ("Cost Cutters",            ["beauty", "hair"]),
    ("Roosters Men's Grooming", ["beauty", "hair"]),
    ("Floyd's 99 Barbershop",   ["beauty", "hair"]),
    ("Drybar",                  ["beauty", "hair"]),
    ("European Wax Center",     ["beauty", "waxing"]),
    ("Massage Heights",         ["beauty", "wellness"]),

    # Automotive
    ("Midas",                   ["automotive", "repair"]),
    ("Jiffy Lube",              ["automotive", "oil_change"]),
    ("Meineke",                 ["automotive", "repair"]),
    ("Maaco",                   ["automotive", "collision"]),
    ("Firestone",               ["automotive", "tires"]),
    ("Christian Brothers",      ["automotive", "repair"]),
    ("Grease Monkey",           ["automotive", "oil_change"]),
    ("Valvoline",               ["automotive", "oil_change"]),
    ("Monro Auto Service",      ["automotive", "repair"]),
    ("Tuffy Auto Service",      ["automotive", "repair"]),
    ("Take 5 Oil Change",       ["automotive", "oil_change"]),

    # Home Services
    ("Servpro",                 ["home_services", "restoration"]),
    ("ServiceMaster",           ["home_services", "cleaning"]),
    ("Merry Maids",             ["home_services", "cleaning"]),
    ("Molly Maid",              ["home_services", "cleaning"]),
    ("Mr. Handyman",            ["home_services", "repair"]),
    ("Paul Davis",              ["home_services", "restoration"]),
    ("Rainbow International",   ["home_services", "restoration"]),
    ("Two Men and a Truck",     ["home_services", "moving"]),
    ("College Hunks",           ["home_services", "moving"]),
    ("Neighborly",              ["home_services"]),
    ("1-800-GOT-JUNK?",        ["home_services", "junk_removal"]),
    ("Window Genie",            ["home_services"]),
    ("BrightView",              ["home_services", "lawn"]),

    # Real Estate
    ("RE/MAX",                  ["real_estate"]),
    ("Century 21",              ["real_estate"]),
    ("Coldwell Banker",         ["real_estate"]),
    ("Keller Williams",         ["real_estate"]),
    ("ERA",                     ["real_estate"]),
    ("Better Homes and Gardens Real Estate", ["real_estate"]),

    # Financial / Tax
    ("H&R Block",               ["financial", "tax"]),
    ("Liberty Tax",             ["financial", "tax"]),
    ("Jackson Hewitt",          ["financial", "tax"]),

    # Education / Childcare
    ("Kumon",                   ["education", "tutoring"]),
    ("Sylvan Learning",         ["education", "tutoring"]),
    ("Mathnasium",              ["education", "tutoring"]),
    ("Tutor Doctor",            ["education", "tutoring"]),
    ("The Goddard School",      ["education", "childcare"]),
    ("Primrose Schools",        ["education", "childcare"]),
    ("KinderCare",              ["education", "childcare"]),
    ("Kiddie Academy",          ["education", "childcare"]),
    ("Learning Care Group",     ["education", "childcare"]),
    ("Lightbridge Academy",     ["education", "childcare"]),

    # Senior Care
    ("Comfort Keepers",         ["senior_care", "home_care"]),
    ("Home Instead",            ["senior_care", "home_care"]),
    ("Right at Home",           ["senior_care", "home_care"]),
    ("Visiting Angels",         ["senior_care", "home_care"]),
    ("Nurse Next Door",         ["senior_care", "home_care"]),
    ("Senior Helpers",          ["senior_care", "home_care"]),

    # Pets
    ("Camp Bow Wow",            ["pet", "dog_boarding"]),
    ("Dogtopia",                ["pet", "dog_daycare"]),
    ("Zoom Room",               ["pet", "dog_training"]),
    ("Pet Supplies Plus",       ["pet", "retail"]),
    ("Petland",                 ["pet", "retail"]),

    # Business Services / Printing
    ("The UPS Store",           ["business_services", "shipping"]),
    ("PostNet",                 ["business_services", "shipping"]),
    ("Minuteman Press",         ["business_services", "printing"]),
    ("AlphaGraphics",           ["business_services", "printing"]),
    ("Coverall",                ["business_services", "commercial_cleaning"]),
    ("Jan-Pro",                 ["business_services", "commercial_cleaning"]),

    # Hotels / Hospitality
    ("Holiday Inn",             ["hospitality", "hotel"]),
    ("Best Western",            ["hospitality", "hotel"]),
    ("Days Inn",                ["hospitality", "hotel"]),
    ("Hampton Inn",             ["hospitality", "hotel"]),
    ("Wyndham",                 ["hospitality", "hotel"]),
    ("Choice Hotels",           ["hospitality", "hotel"]),
    ("Super 8",                 ["hospitality", "hotel"]),
    ("La Quinta",               ["hospitality", "hotel"]),
    ("Comfort Inn",             ["hospitality", "hotel"]),
    ("Hilton Garden Inn",       ["hospitality", "hotel"]),

    # Convenience
    ("7-Eleven",                ["convenience", "retail"]),
    ("Circle K",                ["convenience", "retail"]),
]


# ── WI DFI FULL LIST SCRAPER ──────────────────────────────────────────────────

async def _save_debug(page, name):
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(DEBUG_DIR / f"{name}.png"))
    (DEBUG_DIR / f"{name}.html").write_text(
        await page.content(), encoding="utf-8", errors="replace")


async def scrape_wi_all(debug=False) -> list[str]:
    """
    Scrape all registered franchisor names from Wisconsin DFI by iterating
    A-Z and paginating through each letter's results.
    Returns a flat list of trade/legal names.
    """
    log("  Scraping WI DFI full registrant list (A-Z)...")
    names: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not debug)
        ctx = await browser.new_context(user_agent=UA)
        page = await ctx.new_page()

        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            log(f"    Letter {letter}...")
            try:
                await page.goto("https://www.wdfi.org/apps/FranchiseSearch/search.aspx",
                                wait_until="domcontentloaded", timeout=30000)

                # Fill search input
                search_box = None
                for sel in ["input[type='text']:visible", "input[type='text']",
                            "input[name*='rade']", "input[name*='ranch']",
                            "input[name*='Search']"]:
                    try:
                        el = page.locator(sel).first
                        if await el.is_visible(timeout=2000):
                            search_box = el
                            break
                    except Exception:
                        continue

                if not search_box:
                    log(f"    [WI] No search box found for letter {letter}")
                    if debug:
                        await _save_debug(page, f"wi_no_box_{letter}")
                    break

                await search_box.fill(letter)

                # Submit
                for sel in ["input[type='submit']", "button[type='submit']",
                            "button:has-text('Search')", "input[value='Search']",
                            "[value='Go']"]:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=1000):
                            await btn.click()
                            break
                    except Exception:
                        continue
                else:
                    await search_box.press("Enter")

                await page.wait_for_load_state("networkidle", timeout=15000)

                # Collect across all pages for this letter
                page_num = 0
                while True:
                    page_num += 1
                    rows = await page.query_selector_all("table tr")
                    for row in rows:
                        cells = await row.query_selector_all("td")
                        if not cells:
                            continue
                        name = (await cells[0].inner_text()).strip()
                        if name and len(name) > 2 and name.lower() not in ("trade name", "name", "franchisor"):
                            names.append(name)
                        if len(cells) > 1:
                            legal = (await cells[1].inner_text()).strip()
                            if legal and len(legal) > 2 and legal.lower() not in ("legal name", "name"):
                                names.append(legal)

                    # Try to click "Next" for pagination
                    next_link = None
                    for sel in ["a:has-text('Next')", "a:has-text('>')",
                                "[title='Next Page']", "a[href*='Page$']"]:
                        try:
                            el = page.locator(sel).last
                            if await el.is_visible(timeout=1000):
                                next_link = el
                                break
                        except Exception:
                            continue

                    if not next_link:
                        break
                    await next_link.click()
                    await page.wait_for_load_state("networkidle", timeout=10000)

            except Exception as e:
                log(f"    [WI] Error on letter {letter}: {e}")
                if debug:
                    await _save_debug(page, f"wi_error_{letter}")
                continue

        await browser.close()

    unique = list(dict.fromkeys(n for n in names if n))
    log(f"  WI DFI: scraped {len(unique)} unique names")
    return unique


# ── CLAUDE HAIKU CATEGORISER ──────────────────────────────────────────────────

def _claude(prompt: str) -> str:
    try:
        r = subprocess.run(
            [CLAUDE_CMD, "-p", prompt, "--model", MODEL],
            capture_output=True, text=True, timeout=CLI_TIMEOUT,
            encoding="utf-8", errors="replace")
        return (r.stdout or "").strip()
    except Exception as e:
        log(f"    Claude error: {e}")
        return ""


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def categorise_batch(names: list[str], cache: dict) -> dict[str, list[str]]:
    """Ask Claude Haiku to categorise a batch of franchise brand names."""
    uncached = [n for n in names if _normalise(n) not in cache]
    if not uncached:
        return {n: cache.get(_normalise(n), []) for n in names}

    # Batch into groups of 40
    BATCH = 40
    for i in range(0, len(uncached), BATCH):
        batch = uncached[i:i + BATCH]
        numbered = "\n".join(f"{j+1}. {b}" for j, b in enumerate(batch))
        prompt = (
            "For each franchise brand below, output exactly one JSON line:\n"
            "{\"name\": \"<name>\", \"tags\": [<1-4 lowercase tags>]}\n\n"
            "Tags must be chosen from: qsr, burger, chicken, pizza, mexican, sandwich, "
            "coffee, bakery, dessert, ice_cream, smoothie, casual_dining, fast_casual, "
            "fitness, gym, boutique, health, wellness, beauty, hair, automotive, "
            "home_services, real_estate, financial, education, childcare, senior_care, "
            "pet, business_services, hospitality, hotel, convenience, retail, other\n\n"
            "Brand list:\n" + numbered + "\n\n"
            "Output ONLY the JSON lines, one per brand, nothing else."
        )
        raw = _claude(prompt)
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
                key = _normalise(obj.get("name", ""))
                if key:
                    cache[key] = obj.get("tags", ["other"])
            except json.JSONDecodeError:
                continue
        time.sleep(0.5)

    return {n: cache.get(_normalise(n), ["other"]) for n in names}


# ── MERGE + SAVE ──────────────────────────────────────────────────────────────

def build_db(extra_names: list[str], cat_cache: dict) -> list[dict]:
    existing_keys = {_normalise(name) for name, _ in SEED}

    new_names = [n for n in extra_names if _normalise(n) not in existing_keys and len(n) > 2]
    new_names = list(dict.fromkeys(new_names))  # dedupe, preserve order

    log(f"  Seed brands: {len(SEED)}")
    log(f"  New from WI DFI: {len(new_names)}")

    # Categorise new brands
    if new_names:
        log(f"  Categorising {len(new_names)} new brands with Claude Haiku...")
        new_cats = categorise_batch(new_names, cat_cache)
    else:
        new_cats = {}

    brands = []
    for name, tags in SEED:
        brands.append({"name": name, "tags": tags, "source": "seed"})
    for name in new_names:
        brands.append({"name": name, "tags": new_cats.get(name, ["other"]), "source": "wi_dfi"})

    return brands


def save_db(brands: list[dict]):
    db = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "count": len(brands),
        "brands": brands,
    }
    DB_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"  Saved {len(brands)} brands to {DB_FILE.name}")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Build/update brand_db.json")
    ap.add_argument("--full", action="store_true",
                    help="Supplement seed with WI DFI live scrape (slower)")
    ap.add_argument("--debug", action="store_true",
                    help="With --full: save screenshots for debugging")
    args = ap.parse_args()

    CAT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    cat_cache: dict = {}
    if CAT_CACHE.exists():
        try:
            cat_cache = json.loads(CAT_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass

    extra_names: list[str] = []
    if args.full:
        log("Phase 1: scraping WI DFI full registrant list...")
        extra_names = asyncio.run(scrape_wi_all(debug=args.debug))
    else:
        log("Seed-only mode (use --full to also scrape WI DFI)")

    log("Building database...")
    brands = build_db(extra_names, cat_cache)

    CAT_CACHE.write_text(json.dumps(cat_cache, indent=2, ensure_ascii=False), encoding="utf-8")
    save_db(brands)
    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
