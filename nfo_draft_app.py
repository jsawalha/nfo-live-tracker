"""
NFO Live Auction — Streamlit app
================================
Three tabs in one app:

  1. Live Tracker  — auto-detects each pick (player / price / GM) from MFL's
                     "Auction Messages" feed and keeps a running board + log.
  2. Draft Board   — a column-per-GM visual, colored by position, fed live.
  3. Par Sheet     — your budgeted 14-slot par sheet, embedded.

Run:  streamlit run nfo_draft_app.py

All the parsing lives in mfl_parse.py; this file is just the UI + the poll loop.
"""
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import mfl_parse as M

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except Exception:
    HAS_AUTOREFRESH = False

HERE = Path(__file__).resolve().parent
DEMO_MSG = ""

st.set_page_config(page_title="NFO Live Auction", page_icon="🏈", layout="wide")

POS_COLOR = {"RB": "#3f8f5b", "WR": "#3b7fc4", "QB": "#d97a2b", "TE": "#7a5aa0"}
POS_RGB = {"RB": (63, 143, 91), "WR": (59, 127, 196), "QB": (217, 122, 43), "TE": (122, 90, 160)}
USER_GM = "ReD PantY NiTe"                       # your team, marked with a ★
TARGETS_DEFAULT = {"RB": 6, "WR": 5, "QB": 2, "TE": 1}   # full-roster targets per position


def _row_tint(pos, tier):
    """Row background = position hue, opacity scaled by tier (T1 strongest, fading out)."""
    rgb = POS_RGB.get(pos)
    if not rgb or not tier:
        return ""
    op = max(0.05, 0.30 - (tier - 1) * 0.035)      # T1 .30 → T8 .06
    return f"rgba({rgb[0]},{rgb[1]},{rgb[2]},{op:.2f})"


def build_par_rows(par_slots, targets, me_players):
    """Greedily match your acquired players to par slots (priciest player → biggest
    same-position slot) and return one display row per slot."""
    from collections import defaultdict
    by_pos = defaultdict(list)
    for p in sorted(me_players, key=lambda x: -x["price"]):
        by_pos[p["pos"]].append(p)
    slots_by_pos = defaultdict(list)
    for i, s in enumerate(par_slots):
        slots_by_pos[s["pos"]].append(i)
    assign = {}
    for pos, idxs in slots_by_pos.items():
        players = by_pos.get(pos, [])
        for j, i in enumerate(sorted(idxs, key=lambda i: -targets[i])):
            if j < len(players):
                assign[i] = players[j]
    rows = []
    for i, s in enumerate(par_slots):
        pl = assign.get(i)
        tgt = targets[i]
        price = pl["price"] if pl else 0
        rows.append({"#": i + 1, "POS": s["pos"], "Target": tgt,
                     "Player": pl["player"] if pl else "—",
                     "Price": price, "Diff": tgt - price})
    return rows


def _style_par(df):
    """Read-only par table: light position tint on POS, green/red tint on Difference."""
    disp = df.rename(columns={"Target": "Target Budget", "Player": "Drafted Player",
                              "Diff": "Difference"})

    def pos_bg(v):
        rgb = POS_RGB.get(v)
        return f"background-color: rgba({rgb[0]},{rgb[1]},{rgb[2]},0.16)" if rgb else ""

    def diff_bg(v):
        if v > 0:
            return "background-color: rgba(22,163,74,0.16)"     # green = under target
        if v < 0:
            return "background-color: rgba(192,57,43,0.16)"     # red = over target
        return ""

    return (disp.style
            .map(pos_bg, subset=["POS"])
            .map(diff_bg, subset=["Difference"])
            .format({"Target Budget": "${:.0f}", "Price": "${:.0f}",
                     "Difference": lambda v: ("−$" if v < 0 else "+$") + f"{abs(v):.0f}"}))


def render_nomination(p, market):
    """Compact 'up for bid' KPI panel: the nominated player + the model's price read
    (live-adjusted if the market has moved for that position, else the mean),
    with the 25th / 75th / 90th percentiles. Theme-robust colors."""
    lab = ('<div style="font-size:.7rem;color:#9ca3af;text-transform:uppercase;'
           'letter-spacing:.04em;">Up for bid</div>')
    if not p:
        return (f'<div style="line-height:1.25;">{lab}'
                '<div style="font-size:1.05rem;color:#9ca3af;">— pick a player —</div></div>')
    pos = p["pos"]
    factor = market.get(pos, 1.0)
    live = round(p["price"] * factor)
    moving = abs(factor - 1) >= 0.03
    head, tag = (f"${live}", "live") if moving else (f'${p["price"]}', "mean")
    badge = (f'<span style="background:{POS_COLOR.get(pos, "#888")};color:#fff;font-size:.58rem;'
             f'font-weight:800;padding:1px 5px;border-radius:4px;vertical-align:middle;">{pos}</span>')
    sold = (f' <span style="color:#ef4444;font-size:.7rem;font-weight:700;">SOLD '
            f'${p.get("paid", 0):.0f}</span>' if p.get("status") == "drafted" else "")
    return (
        f'<div style="line-height:1.22;">{lab}'
        f'<div style="font-size:1.0rem;font-weight:700;color:inherit;white-space:nowrap;'
        f'overflow:hidden;text-overflow:ellipsis;">{p["name"]} {badge}{sold}</div>'
        f'<div><span style="font-size:1.3rem;font-weight:800;color:#6366f1;">{head}</span> '
        f'<span style="font-size:.62rem;color:#9ca3af;text-transform:uppercase;">{tag}</span></div>'
        f'<div style="font-size:.72rem;color:#9ca3af;white-space:nowrap;">'
        f'25th <b style="color:#16a34a;">${p["buy"]}</b> · '
        f'75th <b style="color:inherit;">${p["p75"]}</b> · '
        f'90th <b style="color:#ef4444;">${p["p90"]}</b></div></div>')


def render_live_board(players, tiers, market, budget, on_clock=None, nominated_key=None,
                      pos_filter="ALL", hide_drafted=False, walk_pct="90th",
                      starred=None, stars_only=False):
    """A dense, readable, live board: market bar + tier watch + full price list."""
    starred = set(starred or [])
    def mkt_chip(pos):
        pct = (market.get(pos, 1.0) - 1) * 100
        col = "#c0392b" if pct > 3 else ("#2a78d6" if pct < -3 else "#6b7280")
        word = "HOT" if pct > 3 else ("COLD" if pct < -3 else "flat")
        return (f'<span style="margin-right:16px;"><b style="color:{POS_COLOR[pos]}">{pos}</b> '
                f'<b style="color:{col}">{pct:+.0f}%</b> '
                f'<span style="color:#999;font-size:.75em">{word}</span></span>')

    allpct = (market.get("ALL", 1.0) - 1) * 100
    allcol = "#c0392b" if allpct > 3 else ("#2a78d6" if allpct < -3 else "#374151")
    market_bar = (
        '<div style="background:#f7f7fb;border:1px solid #e5e7eb;border-radius:10px;'
        'padding:9px 14px;margin-bottom:10px;font-size:.92rem;color:#111;">'
        f'<b>MARKET</b> &nbsp; overall <b style="color:{allcol}">{allpct:+.0f}%</b>'
        '&nbsp;&nbsp;·&nbsp;&nbsp;' + "".join(mkt_chip(p) for p in ("RB", "WR", "QB", "TE")) +
        '<span style="color:#999;font-size:.75em;"> &nbsp;(actual ÷ model on drafted players)</span></div>')

    # tier watch — one row per position, a little bar per tier (height = players left)
    tw_rows = []
    for pos in ("RB", "WR", "QB", "TE"):
        cells = []
        for t, d in tiers.get(pos, {}).items():
            left, total = d["left"], (d["total"] or 1)
            pct = int(round(100 * left / total))
            color = "#c0392b" if left == 0 else ("#c98500" if left <= 2 else "#3f8f5b")
            tip = f'T{t} · median ${d["med"]} · {left}/{total} left' + \
                  (f' · ${d["drop"]} cliff below' if d.get("drop") else "")
            cells.append(
                f'<span title="{tip}" style="display:inline-block;width:52px;margin-right:6px;'
                f'text-align:center;vertical-align:top;">'
                f'<span style="font-size:.64rem;color:#555;font-weight:700;">T{t}·{left}</span>'
                f'<span style="display:block;height:7px;background:#eef0f2;border-radius:4px;'
                f'overflow:hidden;margin-top:2px;">'
                f'<span style="display:block;height:100%;width:{pct}%;background:{color};"></span>'
                f'</span></span>')
        tw_rows.append(f'<div style="margin:4px 0;white-space:nowrap;"><b style="color:{POS_COLOR[pos]};'
                       f'display:inline-block;width:30px;vertical-align:top;">{pos}</b>'
                       f'{"".join(cells)}</div>')
    tier_watch = ('<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;'
                  'padding:8px 12px;margin-bottom:10px;font-size:.85rem;">'
                  '<div style="font-size:.68rem;color:#6b7280;text-transform:uppercase;'
                  'letter-spacing:.05em;margin-bottom:4px;">Tier watch — players left</div>'
                  + "".join(tw_rows) + '</div>')

    # on the clock
    clock = ""
    if on_clock:
        clock = (f'<div style="background:#fff8e1;border:1px solid #f0d488;border-radius:10px;'
                 f'padding:8px 14px;margin-bottom:10px;font-size:.95rem;">⏳ <b>{on_clock}</b> '
                 f'is on the clock to nominate next.</div>')

    # ---- the price list --------------------------------------------------------
    POS4 = ("RB", "WR", "QB", "TE")
    single = pos_filter in POS4          # a single position is selected -> group by tier

    def player_row(p):
        pos, drafted = p["pos"], p.get("status") == "drafted"
        adj = round(p["price"] * market.get(pos, 1.0))
        moving = abs(market.get(pos, 1.0) - 1) >= 0.03
        posbadge = (f'<span style="background:{POS_COLOR.get(pos, "#888")};color:#fff;font-size:.66rem;'
                    f'font-weight:800;padding:1px 6px;border-radius:4px;">{pos}</span>')
        tier = p.get("tier")
        tint = _row_tint(pos, tier)
        if drafted:
            diff = p["paid"] - p["price"]
            dcol = "#c0392b" if diff > 0 else ("#0a7d3f" if diff < 0 else "#6b7280")
            status = (f'<span style="color:#374151;">→ {p["gm"]}</span> '
                      f'<b>${p["paid"]:.0f}</b> <span style="color:{dcol};font-size:.85em">'
                      f'({diff:+.0f})</span>')
            rowstyle = (f"background:{tint};" if tint else "") + "opacity:.4;"
            namestyle = "text-decoration:line-through;"
            live_cell = ""
        else:
            status = ""
            if p["key"] == nominated_key:
                rowstyle = "background:#fff7d6;box-shadow:inset 3px 0 0 #eab308;"
            else:
                rowstyle = f"background:{tint};" if tint else ""
            namestyle = ""
            live_cell = (f'<span style="background:#eef2ff;color:#3730a3;font-weight:700;'
                         f'padding:1px 7px;border-radius:5px;">${adj}</span>' if moving
                         else f'<span style="color:#a5a3c9;">${adj}</span>')
        is_star = p["name"] in starred
        star_ic = ""
        if is_star:
            star_ic = '<span style="color:#f59e0b;" title="target">★</span> '
            if drafted:
                rowstyle += "box-shadow:inset 4px 0 0 #f59e0b;"
            elif p["key"] != nominated_key:
                rowstyle = "background:#fff7e6;box-shadow:inset 4px 0 0 #f59e0b;"
        tcell = (f'<b style="color:{POS_COLOR.get(pos, "#666")};font-size:.92rem;">{tier}</b>'
                 if tier else '<span style="color:#c7c7c7;">–</span>')
        walk_val = p["p75"] if walk_pct == "75th" else p["p90"]
        rng = (f'<b style="color:#0a7d3f;">${p["buy"]}</b>'
               f'<span style="color:#c7c7c7;"> – </span>'
               f'<b style="color:#c0392b;">${walk_val}</b>')
        return (
            f'<tr data-pos="{pos}" data-drafted="{int(drafted)}" data-star="{int(is_star)}" '
            f'style="{rowstyle}">'
            f'<td style="text-align:center;">{tcell}</td>'
            f'<td style="color:#6b7280;">{p["adp"]}</td>'
            f'<td style="max-width:150px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'
            f'{namestyle}" title="{p["name"]}">{star_ic}<b>{p["name"]}</b> '
            f'<span style="color:#9ca3af;font-size:.85em;">{p["team"]}</span></td>'
            f'<td>{posbadge}</td>'
            f'<td style="text-align:right;">{live_cell}</td>'
            f'<td style="text-align:right;color:#374151;">${p["price"]}</td>'
            f'<td style="text-align:right;white-space:nowrap;">{rng}</td>'
            f'<td style="white-space:nowrap;">{status}</td></tr>')

    def tier_header(pos, t):
        """A bold, color-banded divider marking where a tier starts (+ its cliff below)."""
        col = POS_COLOR.get(pos, "#666")
        if t:
            d = tiers.get(pos, {}).get(t, {})
            bits = []
            if d.get("med"):
                bits.append(f'median&nbsp;${d["med"]}')
            if d.get("total"):
                bits.append(f'{d.get("left", 0)}/{d["total"]} left')
            meta = ' · '.join(bits)
            cliff = (f' &nbsp;·&nbsp; <b style="color:#c0392b;">${d["drop"]} cliff below</b>'
                     if d.get("drop") else '')
            label = f'TIER&nbsp;{t}'
        else:
            meta, cliff, label = '', '', 'UNRANKED'
        return (
            f'<tr class="thdr"><td colspan="8" style="background:linear-gradient(90deg,{col}26,{col}08);'
            f'border-top:3px solid {col};padding:5px 10px;font-weight:800;color:{col};'
            f'font-size:.8rem;letter-spacing:.03em;">▸ {label}'
            f'<span style="font-weight:600;color:#6b7280;font-size:.92em;">'
            f'{("  ·  " + meta) if meta else ""}{cliff}</span></td></tr>')

    body = []
    if single:
        pos = pos_filter
        pool = [p for p in players if p["pos"] == pos]
        if hide_drafted:
            pool = [p for p in pool if p.get("status") != "drafted"]
        if stars_only:
            pool = [p for p in pool if p["name"] in starred]
        pool.sort(key=lambda x: (x.get("tier") or 999, x["adp"]))    # tier order, ADP within
        cur = "___init___"
        for p in pool:
            t = p.get("tier")
            if t != cur:                                             # new tier -> divider band
                cur = t
                body.append(tier_header(pos, t))
            body.append(player_row(p))
    else:
        body = [player_row(p) for p in sorted(players, key=lambda x: x["adp"])]

    table = (
        '<style>.lb td{padding:6px 9px;} .lb th{padding:8px 9px;} '
        '.lb tbody tr{border-bottom:1px solid #eef0f2;}</style>'
        '<div style="max-height:620px;overflow-y:auto;border:1px solid #e5e7eb;border-radius:10px;">'
        '<table class="lb" style="width:100%;border-collapse:collapse;font-size:.98rem;color:#111;">'
        '<thead><tr style="position:sticky;top:0;background:#f3f4f6;color:#374151;font-size:.72rem;'
        'text-transform:uppercase;letter-spacing:.04em;">'
        '<th style="text-align:center;">Tier</th>'
        '<th style="text-align:left;">ADP</th><th style="text-align:left;">Player</th>'
        '<th style="text-align:left;">Pos</th>'
        '<th style="text-align:right;">Live</th>'
        '<th style="text-align:right;">Mean</th>'
        f'<th style="text-align:right;">Buy&ndash;Walk <span style="font-weight:400;'
        f'text-transform:none;">({walk_pct})</span></th>'
        '<th style="text-align:left;">Drafted by</th></tr></thead>'
        '<tbody>' + "".join(body) + '</tbody></table></div>')

    # ALL mode: hide-drafted / targets-only are applied client-side so they persist across
    # the auto-refresh. Single-position mode is already filtered + tier-grouped server-side.
    js = ''
    if not single:
        js = ('<script>'
              f'var H={"true" if hide_drafted else "false"},'
              f'S={"true" if stars_only else "false"};'
              'document.querySelectorAll("tbody tr").forEach(function(r){'
              'if(!r.dataset.pos)return;'
              'var ok=!(H&&r.dataset.drafted=="1")&&!(S&&r.dataset.star!="1");'
              'r.style.display=ok?"":"none";});</script>')

    # a white card so it stays readable regardless of Streamlit's light/dark theme
    return ('<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#111;'
            'background:#ffffff;padding:14px;border-radius:12px;">'
            + market_bar + tier_watch + clock + table + js + '</div>')


def render_needs(gms, targets, user_gm=None):
    """
    Opponents' remaining roster needs = max(0, target - drafted) per position.
    A stacked bar per GM (colored by position) + $ left, sorted by money then need,
    so you see who's forced to bid on what.
    """
    data = []
    for name, t in gms.items():
        need = {p: max(0, targets[p] - t["pos_counts"].get(p, 0)) for p in ("RB", "WR", "QB", "TE")}
        data.append((name, t, need, sum(need.values())))
    if not data:
        return ""
    maxtot = max(1, max(d[3] for d in data))
    # bid pressure = budget × total need — who can spend AND is forced to
    data.sort(key=lambda d: -(d[1]["remaining"] * d[3]))

    body = []
    for name, t, need, tot in data:
        segs = "".join(
            f'<span title="{p}: needs {need[p]}" style="width:{need[p] / maxtot * 100:.1f}%;'
            f'background:{POS_COLOR[p]};height:100%;display:inline-block;"></span>'
            for p in ("RB", "WR", "QB", "TE") if need[p] > 0)
        needtxt = " ".join(f'<b style="color:{POS_COLOR[p]};">{p}{need[p]}</b>'
                           for p in ("RB", "WR", "QB", "TE") if need[p] > 0) \
            or '<span style="color:#9ca3af;">roster full</span>'
        star = ' <span style="color:#eab308;">★</span>' if name == user_gm else ''
        body.append(
            f'<div style="display:flex;align-items:center;gap:8px;margin:3px 0;">'
            f'<div style="width:150px;font-size:.82rem;font-weight:600;color:#111;white-space:nowrap;'
            f'overflow:hidden;text-overflow:ellipsis;">{name}{star}</div>'
            f'<div style="width:46px;text-align:right;font-size:.82rem;font-weight:800;color:#16a34a;">'
            f'${t["remaining"]:.0f}</div>'
            f'<div style="flex:1;height:15px;background:#f1f3f5;border-radius:4px;overflow:hidden;'
            f'display:flex;">{segs}</div>'
            f'<div style="width:150px;font-size:.74rem;white-space:nowrap;">{needtxt}</div></div>')

    legend = "".join(
        f'<span style="margin-right:12px;"><span style="display:inline-block;width:10px;height:10px;'
        f'border-radius:2px;background:{POS_COLOR[p]};vertical-align:middle;"></span> {p}</span>'
        for p in ("RB", "WR", "QB", "TE"))
    tgt = "·".join(f'{p}{targets[p]}' for p in ("RB", "WR", "QB", "TE"))
    return ('<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#fff;'
            'color:#111;padding:12px 14px;border-radius:12px;">'
            f'<div style="font-size:.7rem;color:#6b7280;margin-bottom:8px;">target {tgt} · '
            f'sorted by bid pressure (budget × need) &nbsp;&nbsp; {legend}</div>'
            + "".join(body) + '</div>')


# ============================================================ sidebar / config
with st.sidebar:
    st.header("⚙️ Configuration")
    source = st.radio("Data source",
                      ["Demo (paste messages)", "Mock draft feed (file)", "Live MFL room"])
    budget = st.number_input("Starting budget ($)", value=200, step=25)
    with st.expander("Roster need targets"):
        targets = {p: st.number_input(p, 1, 8, TARGETS_DEFAULT[p], key=f"tgt_{p}")
                   for p in ("RB", "WR", "QB", "TE")}

    room_url = cookie = demo_text = feed_path = ""
    if source == "Live MFL room":
        room_url = st.text_input(
            "Auction room / league URL",
            value="",
            help="Any URL containing L=<league id> works — live OR completed. The app "
                 "reads the full auction from MFL's export API, so a finished room still "
                 "gives you every pick. The year/server number changes each season.")
        cookie = st.text_input("MFL_USER_ID cookie (needed for private leagues)", type="password")
    elif source == "Mock draft feed (file)":
        feed_path = st.text_input("Feed file", value=str(HERE / "nfo_feed.txt"),
                                  help="The file your mock draft's 🔗 live-link writes to. Point both "
                                       "at the same file and this reads it every refresh — no pasting.")
    else:
        demo_text = st.text_area("Auction Messages text", value=DEMO_MSG, height=180,
                                 help="Paste the room's Auction Messages here to test the tracker.")

    refresh = st.slider("Refresh every (seconds)", 3, 15, 5)
    tracking = st.toggle("🔴 Live tracking", value=(source != "Demo (paste messages)"))
    if st.button("🗑️ Reset draft log"):
        st.session_state.pop("sales", None)
        st.rerun()

if tracking and HAS_AUTOREFRESH:
    st_autorefresh(interval=refresh * 1000, key="auto")


# ============================================================ fetch + parse
# Demo mode parses pasted Auction Messages. Live mode pulls the authoritative
# auctionResults export (works live AND on a finished room) for the sales, then
# best-effort reads the room's Auction Messages for the 'on the clock' prompt.
room_html, on_clock, franchises, live_nom = "", None, [], ""
if source == "Demo (paste messages)":
    new_sales, on_clock = M.parse_messages(demo_text)
    sales = M.merge_sales(st.session_state.get("sales", []), new_sales)
elif source == "Mock draft feed (file)":
    blob = ""
    try:
        blob = Path(feed_path).read_text(encoding="utf-8")
    except Exception:
        pass
    sales, on_clock = M.parse_messages(blob)     # feed is cumulative, so this is the full draft
else:
    # live path: MFL's PUBLIC live XML feed (updates the instant a player is won,
    # gives on-clock + current nomination). Fall back to the export for a finished
    # league whose live feed has been removed.
    sales, on_clock, live_nom, franchises = M.fetch_live_auction(room_url, cookie)
    if not sales and not franchises:
        sales = M.fetch_auction_results(room_url, cookie)
        franchises = M._LAST_FRANCHISES

st.session_state["sales"] = sales
gms = M.summarize(sales, all_gms=franchises, budget=budget)
# match your team name case-insensitively — live rooms may store it lowercased
# ("red panty nite") vs the display constant ("ReD PantY NiTe").
user_gm = next((g for g in gms if g.strip().lower() == USER_GM.strip().lower()), USER_GM)

# the live board: reuse the model board's own prices + tiers, marked up with the
# live sales so drafted players fall off and the market/tiers update automatically.
# look next to the app (flat deploy repo) and one dir up (the dev repo layout).
board_path = next((p for p in (HERE / "nfo_2026_draft_board.html",
                               HERE.parent / "nfo_2026_draft_board.html") if p.exists()), None)
board_players, tmeta = M.load_board(str(board_path)) if board_path else ([], {})
board_players, unmatched = M.apply_live(board_players, sales)
market = M.market_factors(board_players)
tiers = M.tier_counts(board_players, tmeta)


# ============================================================ header
st.title("🏈 NFO Live Auction")

# 🎯 who's up for bid — from the live XML feed's currentPlayer (set by fetch_live_auction).
bp_by_norm = {M._norm(p["name"]): p for p in board_players}
nom_player = bp_by_norm.get(M._norm(live_nom)) if live_nom else None
if live_nom and not nom_player:    # detected but not on the 235-player model board (K/DST/deep)
    st.caption(f"🔴 Up for bid: **{live_nom}** — not on the model board (no price read).")

# par plan: session-saved target edits override the file defaults; match your picks to slots
par_file = next((p for p in (HERE / "nfo_2026_par_sheet.html",
                             HERE.parent / "nfo_2026_par_sheet.html") if p.exists()), None)
par_slots = M.load_par_slots(str(par_file)) if par_file else []
me = gms.get(user_gm, {"spent": 0, "count": 0, "players": []})
par_targets = st.session_state.get("par_targets")
if not par_targets or len(par_targets) != len(par_slots):
    par_targets = [s.get("b", 0) for s in par_slots]
par_rows = build_par_rows(par_slots, par_targets, me["players"])
planned = sum(r["Target"] for r in par_rows if r["Player"] != "—")
par_var = planned - me["spent"]                                  # + = under plan, − = over

spent = sum(s["price"] for s in sales)
c1, c2, c3, c4, c5 = st.columns([1, 1, 2.4, 1, 1.3])
c1.metric("Players sold", len(sales))
c2.metric("Total spent", f"${spent:,.0f}")
c3.markdown(render_nomination(nom_player, market), unsafe_allow_html=True)
c4.metric("Updated", datetime.now().strftime("%I:%M:%S %p"))
c5.metric("Par variance", ("−" if par_var < 0 else "+") + f"${abs(par_var):.0f}",
          "over plan" if par_var < 0 else ("under plan" if par_var > 0 else "on plan"),
          delta_color="off")
_clk = f" · on the clock to nominate: **{on_clock}**" if on_clock else ""
st.caption((f"🔴 Live — refreshing every {refresh}s" if tracking else "⏸️ Tracking paused") + _clk)

if source == "Live MFL room":      # engine marker + raw live readout (confirms code version)
    st.sidebar.caption(f"🛰️ live-xml engine · detected up-for-bid: **{live_nom or '—'}**")

if source == "Live MFL room" and not sales:
    if franchises:      # connected fine — the auction just hasn't sold anyone yet
        st.info(f"Connected to the room ({len(franchises)} teams). No players sold yet — "
                "picks will stream in here as they're won."
                + (f"  ·  Up for bid: **{live_nom}**" if live_nom else ""))
    else:
        st.warning(
            "Couldn't reach that auction. Check the league id (L=…), the year and server "
            "number (www##) in the URL, and — for a private league — paste your "
            "MFL_USER_ID cookie. You can always test with Demo mode.")

tab_live, tab_board = st.tabs(["📟 Live Tracker", "🧾 Draft Board"])

# ------------------------------------------------------------ Tab 1: live board
with tab_live:
    if board_players:
        if sales:
            last = sales[-1]
            st.caption(f"Last pick: **{last['player']}** → {last['gm']} for ${last['price']:.0f}"
                       + (f"   ·   {len([p for p in board_players if p.get('status')=='drafted'])} of "
                          f"{len(board_players)} board players off the board"))

        with st.expander("🎯 My par sheet — budget plan", expanded=False):
            if par_slots:
                editing = st.checkbox("✏️ Adjust target budgets", key="par_edit_mode")
                if editing:
                    edited = st.data_editor(
                        pd.DataFrame(par_rows), hide_index=True, use_container_width=True,
                        key="par_editor",
                        column_config={
                            "#": st.column_config.NumberColumn(width="small", disabled=True),
                            "POS": st.column_config.TextColumn(width="small", disabled=True),
                            "Target": st.column_config.NumberColumn(
                                "Target Budget", format="$%d", min_value=0, step=1),
                            "Player": st.column_config.TextColumn("Drafted Player", disabled=True),
                            "Price": st.column_config.NumberColumn(
                                format="$%d", width="small", disabled=True),
                            "Diff": st.column_config.NumberColumn(
                                "Difference", format="$%d", width="small", disabled=True),
                        })
                    plan_total = int(edited["Target"].sum())
                    if st.button("💾 Save allocation changes", key="par_save"):
                        st.session_state["par_targets"] = [int(x) for x in edited["Target"]]
                        st.rerun()
                else:
                    plan_total = sum(r["Target"] for r in par_rows)
                    st.table(_style_par(pd.DataFrame(par_rows)))
                st.caption(
                    f"Plan **${plan_total} / ${budget:.0f}** · spent **${me['spent']:.0f}** · "
                    f"par variance **{'−' if par_var < 0 else '+'}${abs(par_var):.0f}** "
                    f"({'over' if par_var < 0 else 'under' if par_var > 0 else 'on'} plan)")
            else:
                st.caption("nfo_2026_par_sheet.html not found one folder up (the repo "
                           "root). Run build_par_sheet.py to generate it.")

        # ⭐ target list — searchable multiselect (survives the auto-refresh via session key).
        # options always include already-starred names so drafted targets don't vanish.
        avail = [p["name"] for p in sorted(board_players, key=lambda x: x["adp"])]
        star_opts = sorted(set(avail) | set(st.session_state.get("star_targets", [])))
        starred = st.multiselect("⭐ Target players (type to search, then star)", star_opts,
                                 key="star_targets")

        # board controls as Streamlit widgets so they survive the auto-refresh
        fc1, fc2, fc3, fc4 = st.columns([3, 1, 1, 1])
        pos_filter = fc1.radio("Filter", ["ALL", "RB", "WR", "QB", "TE"], horizontal=True,
                               key="pos_filter", label_visibility="collapsed")
        hide_drafted = fc2.checkbox("Hide drafted", key="hide_drafted")
        stars_only = fc3.checkbox("⭐ Targets only", key="stars_only")
        walk_pct = fc4.radio("Walk", ["90th", "75th"], horizontal=True,
                             key="walk_pct", label_visibility="collapsed")
        components.html(
            render_live_board(board_players, tiers, market, budget, on_clock=on_clock,
                              nominated_key=(nom_player["key"] if nom_player else None),
                              pos_filter=pos_filter, hide_drafted=hide_drafted, walk_pct=walk_pct,
                              starred=starred, stars_only=stars_only),
            height=840, scrolling=True)
    else:
        st.warning("The live board needs **nfo_2026_draft_board.html** one folder up "
                   "(the repo root). Run build_draft_board.py to generate it.")

    with st.expander("Per-GM spend", expanded=False):
        rows = sorted(gms.items(), key=lambda kv: -kv[1]["spent"])
        st.dataframe(pd.DataFrame([{
            "GM": name, "Spent": f"${t['spent']:.0f}", "Left": f"${t['remaining']:.0f}",
            "Players": t["count"],
            "RB": t["pos_counts"].get("RB", 0), "WR": t["pos_counts"].get("WR", 0),
            "QB": t["pos_counts"].get("QB", 0), "TE": t["pos_counts"].get("TE", 0),
        } for name, t in rows]), use_container_width=True, hide_index=True)

    with st.expander("Full draft log", expanded=False):
        if sales:
            ldf = pd.DataFrame([{
                "#": i + 1, "Player": s["player"], "Tm": s["team"], "Pos": s["pos"],
                "GM": s["gm"], "Price": f"${s['price']:.0f}", "Time": s.get("time", ""),
            } for i, s in enumerate(sales)][::-1])
            st.dataframe(ldf, use_container_width=True, hide_index=True, height=420)
        else:
            st.caption("No picks yet — sales stream in here as the auction runs.")

    if unmatched:
        with st.expander(f"Picks not on the board ({len(unmatched)}) — DST / K / deep / off-season",
                         expanded=False):
            st.write(", ".join(f"{u['player']} (${u['price']:.0f} · {u['gm']})" for u in unmatched))

# ------------------------------------------------------------ Tab 2: board
with tab_board:
    with st.expander("🎯 Opponents’ remaining roster needs", expanded=True):
        st.markdown(render_needs(gms, targets, user_gm=user_gm), unsafe_allow_html=True)
    n = max(len(gms), 1)
    legend = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:14px;color:#111;">'
        f'<span style="width:12px;height:12px;border-radius:3px;background:{M.POS_COLORS[p]};'
        f'display:inline-block;"></span>{p}</span>' for p in ["QB", "RB", "WR", "TE"])
    cols = []
    for name, t in sorted(gms.items(), key=lambda kv: kv[0].lower()):
        pct = min(100, 100 * t["spent"] / budget) if budget else 0
        cards = "".join(
            f'<div style="background:{M.POS_COLORS.get(p["pos"], M.DEFAULT_COLOR)};border-radius:6px;'
            f'padding:4px 6px;margin-bottom:5px;color:#fff;">'
            f'<div style="font-weight:700;font-size:.7rem;line-height:1.1;">{p["player"]}</div>'
            f'<div style="display:flex;justify-content:space-between;margin-top:2px;font-size:.62rem;'
            f'opacity:.95;"><span>{p["team"]} · {p["pos"]}</span>'
            f'<span style="font-weight:800;">${p["price"]:.0f}</span></div></div>'
            for p in t["players"])
        cards += "".join('<div style="border:1px dashed #d1d5db;border-radius:6px;height:30px;'
                         'margin-bottom:5px;"></div>' for _ in range(max(0, 14 - t["count"])))
        cols.append(
            f'<div style="min-width:0;">'
            f'<div style="border-bottom:2px solid #e5e7eb;margin-bottom:6px;padding-bottom:5px;">'
            f'<div style="font-weight:800;font-size:.72rem;text-align:center;color:#111;'
            f'line-height:1.1;min-height:2.4em;display:flex;align-items:center;'
            f'justify-content:center;overflow-wrap:anywhere;">{name}</div>'
            f'<div style="text-align:center;font-size:1rem;font-weight:800;color:#16a34a;">'
            f'${t["remaining"]:.0f}</div>'
            f'<div style="text-align:center;font-size:.6rem;color:#6b7280;">left · '
            f'${t["spent"]:.0f} spent</div>'
            f'<div style="height:4px;background:#e5e7eb;border-radius:2px;overflow:hidden;'
            f'margin-top:4px;"><div style="height:100%;width:{pct:.0f}%;background:#111827;'
            f'opacity:.5;"></div></div></div>{cards}</div>')
    # white card + a 10-across grid so every team fits with no horizontal scroll
    st.markdown(
        f'<div style="background:#fff;color:#111;padding:12px;border-radius:12px;">'
        f'<div style="margin-bottom:10px;">{legend}</div>'
        f'<div style="display:grid;grid-template-columns:repeat({n},minmax(0,1fr));'
        f'gap:6px;align-items:start;">{"".join(cols)}</div></div>',
        unsafe_allow_html=True)

# ------------------------------------------------------------ poll loop
if tracking and not HAS_AUTOREFRESH:
    time.sleep(refresh)
    st.rerun()
