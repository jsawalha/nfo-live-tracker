"""
mfl_parse.py — pure parsing helpers for the NFO live-auction app.

No Streamlit in here on purpose: everything is a plain function so it can be
unit-tested and reused. The Streamlit app (nfo_draft_app.py) imports from this.

The heart of it is parse_messages(): it turns MFL's live "Auction Messages"
text into structured sales. Everything else (board, per-GM spend, on-the-clock)
derives from that single list of sales.
"""
import html as _html
import re
from datetime import datetime

import requests


def _lst(x):
    """MFL returns a single dict when there's one item, a list when many."""
    return [] if x is None else (x if isinstance(x, list) else [x])


# franchise names captured from the most recent fetch_auction_results() call,
# so the app can show all teams (even ones with no picks yet) without a re-fetch.
_LAST_FRANCHISES = []

# ---- position styling (shared with the board) -----------------------------
POS_COLORS = {
    "QB": "#d97a2b",  # orange
    "RB": "#3f8f5b",  # green
    "WR": "#3b7fc4",  # blue
    "TE": "#7a5aa0",  # purple
    "PK": "#0f9b8e", "K": "#0f9b8e",
    "DEF": "#5b6b7b", "DST": "#5b6b7b",
}
DEFAULT_COLOR = "#6b7280"
POS_ORDER = ["QB", "RB", "WR", "TE", "K", "PK", "DEF", "DST"]

# ---- auction-message grammar ----------------------------------------------
# A completed sale looks like:
#   "Tuten, Bhayshul JAC RB won by Basaraville Xtreme for $2.00 [10:19:39 p.m.]"
# and is followed by:
#   "Eskimo Bros can nominate the next player for auction. [10:19:39 p.m.]"
_TS = r'\[\s*\d{1,2}:\d{2}:\d{2}\s*[apAP]\.?[mM]\.?\s*\]'
_SALE = re.compile(
    r'^\s*(?P<player>.+?)\s+'                       # "St. Brown, Amon-Ra"
    r'(?P<team>[A-Z]{2,3}|FA)\s+'                   # DET / FA
    r'(?P<pos>QB|RB|WR|TE|PK|K|DEF|DST)\s+'         # WR
    r'won by\s+(?P<gm>.+?)\s+for\s+\$(?P<price>[\d,]+(?:\.\d+)?)')
_NOM = re.compile(r'(?P<gm>.+?)\s+can nominate the next player', re.I)


def _clean(text):
    """Strip HTML tags + unescape entities + collapse whitespace."""
    text = re.sub(r'<[^>]+>', ' ', text or '')
    text = _html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def parse_messages(blob):
    """
    Parse the Auction Messages blob into (sales, on_clock).

      sales   -> list of {player, team, pos, gm, price, time} in log order
      on_clock-> name of the GM prompted to nominate next (or None)

    Robust to HTML in the div and to multiple messages concatenated together.
    """
    blob = _clean(blob)
    sales, on_clock = [], None
    for m in re.finditer(r'(.+?)\s*(' + _TS + r')', blob, re.S):
        seg, ts = m.group(1).strip(), m.group(2).strip('[] ')
        s = _SALE.search(seg)
        if s:
            sales.append({
                "player": s["player"].strip(),
                "team": s["team"],
                "pos": s["pos"].upper(),
                "gm": s["gm"].strip(),
                "price": float(s["price"].replace(",", "")),
                "time": ts,
            })
            continue
        n = _NOM.search(seg)
        if n:
            on_clock = n["gm"].strip()
    return sales, on_clock


def merge_sales(prev, new):
    """
    Merge a freshly-parsed sales list into the running one, keyed by player so
    a truncated/rolling message log never loses earlier picks. Preserves the
    order a player was first seen; a later price for the same player wins.
    """
    by_player = {s["player"]: s for s in prev}
    order = [s["player"] for s in prev]
    for s in new:
        if s["player"] not in by_player:
            order.append(s["player"])
        by_player[s["player"]] = s
    return [by_player[p] for p in order]


# ---- pulling the messages out of a room page -------------------------------
def extract_draft_status(html):
    """
    Return the inner HTML of the room's <div id="draft_status"> (the Auction
    Messages panel). Empty string if it isn't in this HTML — MFL sometimes fills
    it via a background request, in which case use a feed URL or paste demo text.
    """
    m = re.search(r'<div[^>]*id="draft_status"[^>]*>(.*?)</div>', html or "", re.S | re.I)
    return m.group(1) if m else ""


# the live nomination lives in the Auction Timer's status cell, e.g.:
#   <td ... id="auction_status" ...>Bidding on <a ...>Chase, Ja'Marr CIN WR</a></td>
_NOM_PLAYER = re.compile(
    r'id="auction_status"[^>]*>.*?Bidding on\s*<a[^>]*>(?P<txt>.*?)</a>', re.S | re.I)


def extract_nominated_player(html):
    """The player currently up for bid, from the room's #auction_status cell.
    Returns the name with the trailing 'TEAM POS' stripped (e.g. 'Chase, Ja'Marr'),
    or '' when nothing is being bid on."""
    m = _NOM_PLAYER.search(html or "")
    if not m:
        return ""
    txt = _clean(m.group("txt"))
    return re.sub(r'\s+[A-Z]{2,3}\s+(QB|RB|WR|TE|PK|K|DEF|DST)\s*$', '', txt).strip()


def fetch(url, cookie_val=None, timeout=10):
    """GET a URL with the optional MFL_USER_ID cookie; return text ('' on error)."""
    try:
        r = requests.get(url, cookies=({"MFL_USER_ID": cookie_val} if cookie_val else None),
                         timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception:
        return ""


# ---- the authoritative source: MFL's auctionResults export -----------------
# Works BOTH live (it updates during the draft) and after the room closes, so
# this is how you pull "everything in the room" from a completed auction.
def _league_from_url(url):
    host = re.search(r'https?://([^/]+)', url or "")
    year = re.search(r'/(\d{4})/', url or "")
    lid = re.search(r'[?&]L=(\d+)', url or "")
    return (host.group(1) if host else "www.myfantasyleague.com",
            year.group(1) if year else str(datetime.now().year),
            lid.group(1) if lid else "")


def fetch_auction_results(url, cookie_val=None):
    """
    Pull the COMPLETE auction via MFL's export API and return the same sales
    schema parse_messages() produces: [{player, team, pos, gm, price, time}].
    Joins auctionResults (bids) x players (names) x league (GM names).
    Returns [] if the URL has no league id or the export can't be read.
    """
    host, year, lid = _league_from_url(url)
    if not lid:
        return []
    base = f"https://{host}/{year}/export"
    ck = {"MFL_USER_ID": cookie_val} if cookie_val else None
    hd = {"User-Agent": "Mozilla/5.0"}

    def _get(typ):
        try:
            r = requests.get(f"{base}?TYPE={typ}&L={lid}&JSON=1", cookies=ck, headers=hd, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception:
            return {}

    players = {p["id"]: p for p in _lst(_get("players").get("players", {}).get("player", []))}
    global _LAST_FRANCHISES
    fmap = {f["id"]: f["name"]
            for f in _lst(_get("league").get("league", {}).get("franchises", {}).get("franchise", []))}
    _LAST_FRANCHISES = list(fmap.values())          # exact GM names, for the board's empty columns
    unit = _get("auctionResults").get("auctionResults", {}).get("auctionUnit", {})

    items = []
    for u in _lst(unit):
        items += _lst(u.get("auction", []))
    items.sort(key=lambda a: int(a.get("timeStarted", 0) or 0))   # draft order

    sales = []
    for a in items:
        pid, bid, fid = a.get("player"), a.get("winningBid"), a.get("franchise")
        if not pid or bid in (None, ""):
            continue
        p = players.get(pid, {})
        sales.append({
            "player": p.get("name", f"Player {pid}"),
            "team": p.get("team", ""), "pos": p.get("position", ""),
            "gm": fmap.get(fid, f"Franchise {fid}"),
            "price": float(bid), "time": a.get("lastBidTime", ""),
        })
    return sales


def _clock(ts):
    """Unix seconds -> '2:41:35 PM' (best-effort)."""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%I:%M:%S %p").lstrip("0")
    except Exception:
        return ""


_EXPORT_MAPS = {}      # (host,year,lid) -> (players_by_id, franchise_names_by_id)


def _export_maps(host, year, lid, ck, hd):
    """(players_by_id, franchise_names_by_id, base_url) from the public exports,
    cached per league so the big players export isn't re-pulled on every live poll.
    base_url comes from the league export itself, so the live XML is fetched from the
    correct MFL server (www##) even if the typed room URL had the wrong/absent host."""
    key = (host, year, lid)
    if key not in _EXPORT_MAPS:
        def _json(typ):
            try:
                r = requests.get(f"https://{host}/{year}/export?TYPE={typ}&L={lid}&JSON=1",
                                 cookies=ck, headers=hd, timeout=20)
                r.raise_for_status()
                return r.json()
            except Exception:
                return {}
        lg = _json("league").get("league", {})
        players = {p["id"]: p for p in _lst(_json("players").get("players", {}).get("player", []))}
        fmap = {f["id"]: f["name"] for f in _lst(lg.get("franchises", {}).get("franchise", []))}
        base = (lg.get("baseURL") or f"https://{host}").rstrip("/")
        if not players or not fmap:
            return players, fmap, base     # don't cache a failed/empty pull — retry next time
        _EXPORT_MAPS[key] = (players, fmap, base)
    return _EXPORT_MAPS[key]


def fetch_live_auction(url, cookie_val=None):
    """
    Read an IN-PROGRESS (or finished) auction from MFL's PUBLIC live XML feed — the
    same static file the auction room itself polls, so no login is needed and picks
    appear the instant they're won. The auctionResults *export* only fills once the
    auction is finalized, which is why a live room reads as empty there.

        /fflnetdynamic<year>/<league>_LEAGUE_auction_results.xml

    Returns (sales, on_clock, nominated_player, franchises); sales use the same
    schema as fetch_auction_results. Empty tuple values if the feed isn't reachable.
    """
    host, year, lid = _league_from_url(url)
    if not lid:
        return [], None, "", []
    ck = {"MFL_USER_ID": cookie_val} if cookie_val else None
    hd = {"User-Agent": "Mozilla/5.0"}

    # names + the authoritative server come from the exports first; the live XML then
    # gets fetched from that exact server (fixes wrong/missing host in the typed URL).
    players, fmap, base = _export_maps(host, year, lid, ck, hd)
    global _LAST_FRANCHISES
    _LAST_FRANCHISES = list(fmap.values())

    try:
        r = requests.get(f"{base}/fflnetdynamic{year}/{lid}_LEAGUE_auction_results.xml",
                         cookies=ck, headers=hd, timeout=15)
        r.raise_for_status()
        xml = r.text
    except Exception:
        return [], None, "", []
    if "<auctionResults" not in xml:
        return [], None, "", []

    def _attrs(s):
        return dict(re.findall(r'(\w+)="([^"]*)"', s))

    hm = re.search(r'<auctionResults\b([^>]*)>', xml)
    head = _attrs(hm.group(1)) if hm else {}
    on_clock = fmap.get(head.get("currentNominator", ""))
    # 'currentPlayer' on the container is the player actively up for bid (absent between
    # nominations / when paused with nothing live).
    nominated = players.get(head.get("currentPlayer", ""), {}).get("name", "")

    rows = []
    for m in re.finditer(r'<auction\b([^>]*?)/?>', xml):
        a = _attrs(m.group(1))
        if "player" not in a or not a.get("completed"):   # skip <franchise> + the live row
            continue
        p = players.get(a["player"], {})
        rows.append((int(a.get("completed", 0) or 0), {
            "player": p.get("name", f"Player {a['player']}"),
            "team": p.get("team", ""), "pos": (p.get("position", "") or "").upper(),
            "gm": fmap.get(a.get("highBidder", ""), f"Franchise {a.get('highBidder')}"),
            "price": float(a.get("highBid", 0) or 0),
            "time": _clock(a.get("completed")),
        }))
    rows.sort(key=lambda r: r[0])                   # chronological
    return [r[1] for r in rows], on_clock, nominated, list(fmap.values())


# ---- the model board: prices + tiers, reused live -------------------------
def _norm(name):
    """Canonicalize a player name so 'Henderson, TreVeyon' == 'TreVeyon Henderson'."""
    name = (name or "").lower().strip()
    if "," in name:
        last, first = name.split(",", 1)
        name = f"{first.strip()} {last.strip()}"
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", name)
    name = re.sub(r"[^a-z ]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def load_board(path):
    """
    Pull the embedded model data out of nfo_2026_draft_board.html:
      players -> [{adp,pos,posRank,name,team,price,median,p90,p75,buy,tier}, ...]
      tmeta   -> {pos: {tier: {med, drop}}}
    """
    import json as _json
    html = open(path, encoding="utf-8").read()
    m = re.search(r"const DATA = (\[.*?\]);", html, re.S)
    t = re.search(r"const TMETA = (\{.*?\});", html, re.S)
    players = _json.loads(m.group(1)) if m else []
    for p in players:
        p["key"] = _norm(p["name"])
    tmeta = _json.loads(t.group(1)) if t else {}
    return players, tmeta


def load_par_slots(path):
    """Parse the par sheet's DEFAULT_SLOTS ([{g,pos,b}, ...]) — the budget plan."""
    import json as _json
    try:
        html = open(path, encoding="utf-8").read()
    except Exception:
        return []
    m = re.search(r"const DEFAULT_SLOTS = (\[.*?\]);", html, re.S)
    if not m:
        return []
    raw = re.sub(r"([{,]\s*)(\w+):", r'\1"\2":', m.group(1))   # quote bare JS keys
    raw = raw.replace("'", '"')
    try:
        return _json.loads(raw)
    except Exception:
        return []


def apply_live(players, sales):
    """
    Annotate each board player with live status from the auction sales.
    Adds to matched players: status='drafted', paid, gm. Returns (players, unmatched)
    where unmatched are sold players not on the board (DST/kickers/deep guys).
    """
    sold = {}
    for s in sales:
        sold.setdefault(_norm(s["player"]), s)
    matched = set()
    for p in players:
        s = sold.get(p["key"])
        if s:
            p["status"], p["paid"], p["gm"] = "drafted", s["price"], s["gm"]
            matched.add(p["key"])
        else:
            p["status"], p["paid"], p["gm"] = "avail", None, None
    unmatched = [s for k, s in sold.items() if k not in matched]
    return players, unmatched


def market_factors(players, min_model=6):
    """
    Live inflation = sum(actual) / sum(model) over drafted players, overall and
    per position. min_model ignores $1-dart noise. Factor >1 = room running hot.
    """
    agg = {"ALL": [0.0, 0.0]}
    for p in players:
        if p.get("status") == "drafted" and p["price"] >= min_model:
            for k in ("ALL", p["pos"]):
                a = agg.setdefault(k, [0.0, 0.0])
                a[0] += p["paid"]; a[1] += p["price"]
    return {k: (v[0] / v[1] if v[1] else 1.0) for k, v in agg.items()}


def tier_counts(players, tmeta):
    """Per position/tier: how many are still AVAILABLE, plus the tier median + cliff drop."""
    out = {}
    for pos in ("RB", "WR", "QB", "TE"):
        tiers = {}
        for t, meta in (tmeta.get(pos, {})).items():
            left = sum(1 for p in players
                       if p["pos"] == pos and p.get("tier") == int(t) and p.get("status") != "drafted")
            total = sum(1 for p in players if p["pos"] == pos and p.get("tier") == int(t))
            tiers[int(t)] = {"left": left, "total": total,
                             "med": meta.get("med"), "drop": meta.get("drop", 0)}
        if tiers:
            out[pos] = dict(sorted(tiers.items()))
    return out


# ---- room-HTML parsers (used for franchise names + live sync) --------------
def parse_franchises(html):
    out = {}
    for m in re.finditer(
        r"franchiseDatabase\['fid_(\d+)'\]\s*=\s*new Franchise\('\d+',\s*'([^']*)'", html or ""):
        out[m.group(1)] = m.group(2).strip()
    return out


def parse_server_time(html):
    m = re.search(r"currentServerTime\s*=\s*(\d+)", html or "")
    return int(m.group(1)) if m else None


# ---- deriving everything the UI needs from the sales list ------------------
def summarize(sales, all_gms=None, budget=200):
    """
    Roll the flat sales list up into per-GM state.

      returns {gm: {players:[...], spent, remaining, count, pos_counts}}

    all_gms lets you show teams that haven't bought anyone yet (from the room's
    franchise list). If None, only GMs that appear in sales are shown.
    """
    gms = {}
    names = set(all_gms or []) | {s["gm"] for s in sales}
    for name in names:
        gms[name] = {"players": [], "spent": 0.0, "count": 0,
                     "pos_counts": {}, "remaining": budget}
    for s in sales:
        t = gms.setdefault(s["gm"], {"players": [], "spent": 0.0, "count": 0,
                                     "pos_counts": {}, "remaining": budget})
        t["players"].append(s)
        t["spent"] += s["price"]
        t["count"] += 1
        t["pos_counts"][s["pos"]] = t["pos_counts"].get(s["pos"], 0) + 1
    for t in gms.values():
        t["remaining"] = budget - t["spent"]
        t["players"].sort(key=lambda p: (-p["price"],
                                         POS_ORDER.index(p["pos"]) if p["pos"] in POS_ORDER else 99))
    return gms
