"""
Regenerates the game data embedded in store.html from g2bulk's public
catalogue API (https://api.g2bulk.com/v1) — no API key needed, since
games/categories/products/catalogue are all public read endpoints.

What it does:
  1. Fetches /v1/games (every direct top-up game g2bulk offers).
  2. Groups regional/variant duplicates under one game (e.g. all 12
     "Free Fire" country listings become one "Free Fire" entry), but
     — unlike the first version of this script — keeps every working
     variant as a selectable category under that game, instead of
     throwing the others away. A game with only one source listing just
     gets a single "Standard" variant.
  3. For every variant, fetches /v1/games/{code}/catalogue (prices),
     /v1/games/fields (which inputs checkout needs) and, when a server
     id is required, /v1/games/servers (the real server list).
  4. Converts USD prices to MMK at EXCHANGE_RATE.
  5. Pins Mobile Legends to the front of the list and flags it "hot".
  6. Fetches a curated list of gift cards (GIFTCARD_CATEGORY_IDS) from
     /v1/category, tagged type "giftcard" so the frontend can filter them
     separately from games (type "game"). Edit that list to add/remove
     which gift cards show up.
  7. Writes the result back into store.html, between the
     GENERATED:GAMES_JSON markers.

Run it whenever you want to refresh prices/stock, or after changing
EXCHANGE_RATE:

    python3 build_store.py
"""
import json
import re
import collections
import time
import sys

import requests

API = "https://api.g2bulk.com/v1"
STORE_HTML = "store.html"
EXCHANGE_RATE = 4325  # 1 USD in MMK — update as the rate moves
PINNED_FIRST = "mobile legends"  # base name (lowercase) to always show first
HOT_GAMES = {"mobile legends"}   # base names (lowercase) that get a Hot badge

# Games whose display name is a raw internal code, not a real title, or
# that are a confirmed dead-duplicate of a properly-named entry.
MANUAL_EXCLUDE_CODES = {
    "lds_login",          # same game as "lds" / Love and Deepspace, code shown as its name
    "magic_chest_gogo",   # typo'd duplicate of magic_chess_gogo
}

# Some variants' "userid"/"serverid"/"charname" fields don't mean what
# their generic labels suggest — e.g. HoYoverse "Login Mode" top-ups need
# an account email + password, not a public player ID. g2bulk documents
# this inside the free-text `notes` field for that variant. These are the
# exact notes strings that need field relabeling, mapped to the real
# field meaning plus a plain-English replacement for the rest of the note.
FIELD_OVERRIDES_BY_NOTE = {
    "userid = email, serverid = password, charname = genshin userid | ONLY SUPPORTS HOYOVERSE ACCOUNTS": (
        {"userid": ["Email", "email"], "serverid": ["Password", "password"], "charname": ["User ID", "text"]},
        "Only works with official HoYoverse accounts.",
    ),
    "userid = email, serverid = password, charname = hsr userid | ONLY SUPPORTS HOYOVERSE ACCOUNTS": (
        {"userid": ["Email", "email"], "serverid": ["Password", "password"], "charname": ["User ID", "text"]},
        "Only works with official HoYoverse accounts.",
    ),
    "userid = email, serverid = password, charname = zzz userid | ONLY SUPPORTS HOYOVERSE ACCOUNTS": (
        {"userid": ["Email", "email"], "serverid": ["Password", "password"], "charname": ["User ID", "text"]},
        "Only works with official HoYoverse accounts.",
    ),
    "userid = email, serverid = password, charname = server [Asia, Europe, Americas]": (
        {"userid": ["Email", "email"], "serverid": ["Password", "password"], "charname": ["Server", "text"]},
        "",
    ),
    "Email in userid and Password in serverid, charname = Roleid[It looks something like this: 38174111708527549]. "
    "Only allow perfect world login, need to get customer to set password in-game using -> Settings -> User Center -> Reset Password": (
        {"userid": ["Email", "email"], "serverid": ["Password", "password"], "charname": ["Role ID", "text"]},
        "Perfect World login only. If you haven't set an in-game password yet: Settings → User Center → Reset Password.",
    ),
    "Email in userid and Password in serverid, charname = Server [Asia, America, SEA, Europe]. "
    "Only allow perfect world login, need to get customer to set password in-game using -> Settings -> User Center -> Reset Password": (
        {"userid": ["Email", "email"], "serverid": ["Password", "password"], "charname": ["Server", "text"]},
        "Perfect World login only. If you haven't set an in-game password yet: Settings → User Center → Reset Password.",
    ),
    "Email in userid and Password in serverid, only allow netmarble email password login, only netmarble login": (
        {"userid": ["Email", "email"], "serverid": ["Password", "password"]},
        "Only works with Netmarble email/password login.",
    ),
    "Email in userid and Password in serverid, only allow non social media login": (
        {"userid": ["Email", "email"], "serverid": ["Password", "password"]},
        "Social login (Facebook, Google, etc.) isn't supported here — use your email and password instead.",
    ),
    "Available for US and Asia users, charname = Server Name": (
        {"charname": ["Server", "text"]},
        "Only available to US and Asia players.",
    ),
    "Available for all users, serverid = Region [ie: Europe], charname = Server Name [iE: PVE01-00109]": (
        {"serverid": ["Region", "text"], "charname": ["Server ID", "text"]},
        "Available to all players.",
    ),
}

# g2bulk's free-text notes copied verbatim would make this storefront's
# copy read identically to theirs. These are hand-reworded replacements
# for the handful of notes that don't fit the generic "Available for X"
# pattern handled programmatically below.
PLAIN_NOTE_REWRITES = {
    "Applicable for Telegram Premium and Telegram Stars purchase only":
        "Only for Telegram Premium and Telegram Stars purchases.",
    "Available for all users that are registered above 13 and do not have a currency lock in their account":
        "Available to all players aged 13+ without a currency lock on their account.",
    "Character name must match exactly or it would not work":
        "Double-check your character name — it must match exactly.",
    "Mainly use for Bag End players":
        "Primarily for Bag End server players.",
    "Not available for Indonesia users, Indonesian users can use mlbb_global. Not available for SG/MY/PH/RU/VN":
        "Not available to Indonesia, SG, MY, PH, RU or VN players — Indonesian players can use the Global option instead.",
    "Not available for Indonesia users, Indonesian users can use mlbb_global/mlbb_indo":
        "Not available to Indonesia players — try the Global option instead.",
    "Not available for Vietnam, Thailand and Indonesia users, not available for Middle East Users":
        "Not available to Vietnam, Thailand, Indonesia or Middle East players.",
    "Not available to China users, to receive Genesis Crystals, users must be logged in on PC/Android/iOS":
        "Not available to China players — you must be logged in via PC, Android or iOS to receive Genesis Crystals.",
    "Only for Indonesian users":
        "Only available to Indonesian players.",
    "Only valid for Asia users":
        "Only valid for Asia players.",
    "Packages are maintained by Nexon, if the user is not able to purchase it in-game, we will also not be able to "
    "purchase it. If multiple quantities of a package can be bought, the user has to claim the package first before "
    "being able to buy another one. Character name must match exactly or it would not work":
        "Managed directly by Nexon — if you can't buy a package in-game, we can't either. For repeatable "
        "packages, claim the current one before buying the next. Your character name must match exactly.",
    "Please only select the denom corresponding to the platform of the account [Android/iOS]":
        "Pick the option matching your account's platform (Android or iOS).",
    "Servers can be found on NetEase payment page for LifeAfter":
        "Check LifeAfter's NetEase payment page to find your server.",
    "Some users may be under purchase ban, this would reflect as invalid UserID":
        "If your account has a purchase ban, it may show as an invalid User ID.",
    "User has to create an UserID first":
        "You'll need to create a User ID first.",
    "Available for all users [Except SEA]":
        "Available to all players except SEA.",
    "Available for all users | For those that wants this game, please contact Admin before integrating":
        "Available to all players — contact us on Telegram first if you'd like this game added.",
}

_AVAIL_POS_RE = re.compile(r"^(?:Only\s+)?[Aa]vailable\s+(?:only\s+)?(?:for|to)\s+(.+?)(?:\s+only)?$")
_AVAIL_NEG_RE = re.compile(r"^Not\s+available\s+(?:for|to)\s+(.+?)(?:\s+region)?$")
_AVAIL_COMPLEX_RE = re.compile(
    r"\b(can|use|also|will|would|please|receive|charname|serverid|create|match|reflect|maintained|correspond)\b",
    re.IGNORECASE,
)


def reword_note(raw_note):
    """Rewrite g2bulk's own note text into original wording, and return any
    field-label overrides it implies. Never displays their sentence as-is."""
    if not raw_note:
        return "", {}
    if raw_note in FIELD_OVERRIDES_BY_NOTE:
        overrides, text = FIELD_OVERRIDES_BY_NOTE[raw_note]
        return text, overrides
    if raw_note in PLAIN_NOTE_REWRITES:
        return PLAIN_NOTE_REWRITES[raw_note], {}

    is_neg = raw_note.lower().startswith("not available")
    if not _AVAIL_COMPLEX_RE.search(raw_note) and "[" not in raw_note:
        pattern = _AVAIL_NEG_RE if is_neg else _AVAIL_POS_RE
        m = pattern.match(raw_note)
        if m:
            locale = m.group(1).strip().rstrip(".")
            locale = re.sub(r"\busers?\b", "", locale, flags=re.IGNORECASE)
            locale = re.sub(r"\s+", " ", locale).strip().rstrip(",").strip()
            if locale and len(locale) <= 55:
                verb = "Not available to" if is_neg else "Only available to"
                return f"{verb} {locale} players.", {}

    # Unknown shape we haven't seen before — still rewrite generically
    # rather than ever showing their exact sentence.
    return "See Telegram for this plan's exact requirements.", {}


REGION_WORDS = [
    "Middle East", "South Africa", "South Korea", "Saudi Arabia", "New Zealand", "Hong Kong",
    "Czech Republic", "North America", "United States", "Login Mode",
    "Bangladesh", "Brazil", "Cambodia", "Colombia", "Europe", "Global", "Indonesia", "LATAM",
    "Malaysia", "Mexico", "Philippines", "Singapore", "Taiwan", "Thailand", "Vietnam", "Russia",
    "Turkey", "Ukraine", "Poland", "France", "Germany", "Spain", "Italy", "Austria", "Belgium",
    "Finland", "Greece", "Hungary", "Kuwait", "Lebanon", "Oman", "Qatar", "Romania", "Slovakia",
    "Bahrain", "Netherlands", "Canada", "Australia", "Japan", "India", "Switzerland", "Argentina",
    "Ireland", "Login", "Instant", "Worldwide", "Asia", "Americas", "NAEU",
    "MENA", "CIS", "SEA", "SGMY", "SG", "MY", "KH", "PH", "VN", "TH", "ID", "BR", "EU", "NA",
    "UAE", "US", "USA", "UK", "KSA",
]
REGION_WORDS.sort(key=len, reverse=True)
_alt = "|".join(re.escape(w) for w in REGION_WORDS)
_paren_pattern = re.compile(r"\(\s*(?:" + _alt + r")\s*\)", re.IGNORECASE)
_word_pattern = re.compile(r"(?:^|[\s:\-(])(" + _alt + r")(?:$|(?=[\s():]))", re.IGNORECASE)


def base_name(name):
    """Strip region/variant qualifiers to find the underlying game name,
    e.g. 'Freefire Indonesia' and 'Freefire Global' both -> 'Freefire'."""
    n = name
    prev = None
    while prev != n:
        prev = n
        n = _paren_pattern.sub("", n)
        n = _word_pattern.sub("", n)
        n = n.strip(" :-()").strip()
    return re.sub(r"\s+", " ", n).strip()


def rank(member, base_l):
    """Lower tuple sorts first — decides which variant is shown/selected
    by default. Prefer, in order: the plain/unsuffixed name, a 'Global'
    listing, a SEA/Asia/Instant listing, a Singapore listing, else the
    oldest (lowest id) entry."""
    name_l = member["name"].lower()
    exact = name_l == base_l
    is_global = "global" in name_l
    is_neutral = any(w in name_l for w in ("sea", "asia", "instant"))
    is_sg = "singapore" in name_l or member["code"].lower().endswith("_sg")
    return (not exact, not is_global, not is_neutral, not is_sg, member["id"])


def variant_label(name, base_display):
    """Human label for a variant within its game, e.g. 'Mobile Legends
    Brazil' with base 'Mobile Legends' -> 'Brazil'."""
    idx = name.lower().find(base_display.lower())
    if idx == -1:
        return name
    remainder = (name[:idx] + name[idx + len(base_display):]).strip(" :-()").strip()
    return remainder if remainder else "Standard"


def slugify(name):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


# A curated set of well-known, generic gift cards from g2bulk's /v1/category
# catalogue (as opposed to /v1/games, which is games only). These are NOT
# game top-ups — they're store credit for a platform — so they're kept in
# their own "Gift Cards" section rather than mixed into the games grid.
GIFTCARD_CATEGORY_IDS = [86, 3, 19, 5, 16, 126, 4, 14, 160, 17]


def fetch_giftcards(session):
    print("Fetching gift card categories...", file=sys.stderr)
    categories = {c["id"]: c for c in session.get(f"{API}/category", timeout=20).json()["categories"]}
    result = []
    for cid in GIFTCARD_CATEGORY_IDS:
        cat = categories.get(cid)
        if not cat:
            continue
        try:
            products = session.get(f"{API}/category/{cid}", timeout=15).json().get("products", [])
        except requests.RequestException:
            products = []
        time.sleep(0.05)

        denoms = []
        for p in products:
            if p.get("stock") == 0:
                continue
            usd = p.get("unit_price")
            if not isinstance(usd, (int, float)):
                continue
            denoms.append({"n": p.get("title", "").strip(), "u": usd, "m": round(usd * EXCHANGE_RATE)})
        if not denoms:
            continue
        denoms.sort(key=lambda d: d["u"])

        name = cat["title"].strip()
        result.append({
            "name": name,
            "slug": slugify(name),
            "img": cat.get("image_url") or None,
            "type": "giftcard",
            "hot": False,
            "startMmk": denoms[0]["m"],
            "variants": [{
                "label": "Standard",
                "code": f"giftcard_{cid}",
                "denoms": denoms,
                "startMmk": denoms[0]["m"],
                "fields": [],
                "fieldLabels": {},
                "notes": "",
                "servers": None,
            }],
        })
    print(f"  {len(result)}/{len(GIFTCARD_CATEGORY_IDS)} gift cards fetched with stock.", file=sys.stderr)
    return result


def fetch_catalogue(session, code):
    try:
        r = session.get(f"{API}/games/{code}/catalogue", timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("catalogues"):
            return None
        return data
    except requests.RequestException:
        return None


def fetch_fields(session, code):
    try:
        r = session.post(f"{API}/games/fields", json={"game": code}, timeout=15)
        data = r.json()
        info = data.get("info") or {}
        return info.get("fields") or ["userid"], info.get("notes") or ""
    except requests.RequestException:
        return ["userid"], ""


def fetch_servers(session, code):
    try:
        r = session.post(f"{API}/games/servers", json={"game": code}, timeout=15)
        if r.status_code != 200:
            return None
        servers = r.json().get("servers")
        return servers if servers else None
    except requests.RequestException:
        return None


def build_variant(session, member, label):
    data = fetch_catalogue(session, member["code"])
    time.sleep(0.05)
    if not data:
        return None
    denoms = []
    for c in data["catalogues"]:
        usd = c.get("amount")
        if not isinstance(usd, (int, float)):
            continue
        denoms.append({"n": c.get("name", ""), "u": usd, "m": round(usd * EXCHANGE_RATE)})
    denoms.sort(key=lambda d: d["u"])  # cheapest first

    raw_fields, raw_notes = fetch_fields(session, member["code"])
    time.sleep(0.05)
    servers = fetch_servers(session, member["code"]) if "serverid" in raw_fields else None
    if "serverid" in raw_fields:
        time.sleep(0.05)

    note_text, field_overrides = reword_note(raw_notes)

    return {
        "label": label,
        "code": member["code"],
        "denoms": denoms,
        "startMmk": denoms[0]["m"] if denoms else None,
        "fields": raw_fields,
        "fieldLabels": field_overrides,
        "notes": note_text,
        "servers": servers,
    }


def main():
    session = requests.Session()

    print("Fetching game list...", file=sys.stderr)
    games = session.get(f"{API}/games", timeout=20).json()["games"]

    groups = collections.defaultdict(list)
    base_display_by_key = {}
    for g in games:
        if g["code"].lower() == "test" or g["code"] in MANUAL_EXCLUDE_CODES:
            continue
        base_display = base_name(g["name"])
        key = base_display.lower()
        base_display_by_key[key] = base_display
        groups[key].append(g)

    print(f"{len(games)} raw games -> {len(groups)} unique games. Fetching prices/fields...", file=sys.stderr)

    result = []
    skipped = []
    label_collisions_fixed = 0

    for i, (key, members) in enumerate(sorted(groups.items())):
        base_display = base_display_by_key[key]
        ordered = sorted(members, key=lambda m: rank(m, key))

        labels = [variant_label(m["name"], base_display) for m in ordered]
        # Disambiguate any label collision within this game (e.g. two
        # "Standard"-labelled console/pc listings of the same title) using
        # the tail of their code instead.
        seen = collections.Counter(labels)
        for idx, lbl in enumerate(labels):
            if seen[lbl] > 1:
                tail = ordered[idx]["code"].rsplit("_", 1)[-1]
                labels[idx] = tail.upper() if len(tail) <= 3 else tail.capitalize()
                label_collisions_fixed += 1

        variants = []
        for member, label in zip(ordered, labels):
            v = build_variant(session, member, label)
            if v:
                variants.append(v)

        if not variants:
            skipped.append(base_display)
            continue

        result.append({
            "name": base_display,
            "slug": slugify(base_display),
            "img": next((m.get("image_url") for m in ordered if m.get("image_url")), None),
            "type": "game",
            "variants": variants,
            "startMmk": min(v["startMmk"] for v in variants if v["startMmk"] is not None),
            "hot": key in HOT_GAMES,
        })
        if (i + 1) % 20 == 0:
            print(f"  ...{i + 1}/{len(groups)}", file=sys.stderr)

    result.sort(key=lambda g: g["name"].lower())
    pinned = [g for g in result if g["name"].lower() == PINNED_FIRST]
    rest = [g for g in result if g["name"].lower() != PINNED_FIRST]
    result = pinned + rest

    total_variants = sum(len(g["variants"]) for g in result)
    print(
        f"Done. {len(result)} games ({total_variants} variants total, "
        f"{label_collisions_fixed} label collisions resolved). "
        f"Skipped (no working variant): {skipped}",
        file=sys.stderr,
    )

    giftcards = fetch_giftcards(session)
    result = result + giftcards

    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    with open(STORE_HTML, encoding="utf-8") as f:
        html = f.read()

    start_marker = "/*GENERATED:GAMES_JSON_START*/"
    end_marker = "/*GENERATED:GAMES_JSON_END*/"
    start = html.index(start_marker) + len(start_marker)
    end = html.index(end_marker, start)
    html = html[:start] + payload + html[end:]

    with open(STORE_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {len(payload)} bytes of game data into {STORE_HTML}", file=sys.stderr)


if __name__ == "__main__":
    main()
