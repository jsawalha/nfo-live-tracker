# NFO Live Auction Tracker

Streamlit app that tracks the NFO (MyFantasyLeague) live auction draft against a
2026 price model — draft board, par sheet, and live nomination read-out.

## Deploy (Streamlit Community Cloud)
- Entry point: `nfo_draft_app.py`
- Dependencies: `requirements.txt`
- The MFL cookie (for private leagues) is entered in the sidebar at runtime — **no
  credentials are stored in this repo.**

## Run locally
```bash
pip install -r requirements.txt
streamlit run nfo_draft_app.py
```

## Contents
- `nfo_draft_app.py` — the app
- `mfl_parse.py` — MFL live-feed parsing + board/price helpers
- `nfo_2026_draft_board.html` / `nfo_2026_par_sheet.html` — model data the app reads

The two HTML data files are generated upstream (in the `mfo_scrape` project) and
copied in when the model is refreshed.
