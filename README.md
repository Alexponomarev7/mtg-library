# Card Library

A local app that tracks where your physical Magic cards live — which binder or box,
and for binders the exact **page, row and column**.

## Run it

Needs Python 3.8 or newer. Nothing else to install.

```
cd card-library
python3 server.py          # Windows: py server.py
```

Your browser opens at http://127.0.0.1:8765. Stop with Ctrl+C.
The app only listens on your own computer (127.0.0.1).

Options: `--no-browser` to not open a browser; env vars `CARDLIB_PORT` (default 8765)
and `CARDLIB_DB` (default `library.db` next to `server.py`).

## How it works

1. **Binders & boxes** — create your storage.
   - Binder: number of sheets, pockets per page (e.g. 3×3 = 9-pocket), one or both sides, and a filling mode:
     - **Set order**: every card has a fixed pocket by collector number. Empty pockets show the missing card in grey — click it to add.
     - **Fill in order**: each new card goes in the next empty pocket.
   - Box: optional capacity. Cards are grouped in sections by set.
   - **Assign sets** to each binder/box. A binder/box with no sets is "general" and accepts anything.
     In set-order binders you can reorder sets, start each set on a new page, and include extras/variants.
2. **Add cards** — search a name (or `set number`, like `blb 12`), pick the exact printing, and the app
   tells you where it goes: *Page 4 · Row 2 · Column 3*, with a little pocket map. Duplicates of a card
   that already sits in its pocket are sent to a box assigned to that set.
3. **Find** — type a card name to see where every copy is. "Show" opens the binder page with the pocket highlighted.

If you change the set order of a binder, the app shows a list of cards to move physically.

## Data

- Card data and images come from [Scryfall](https://scryfall.com) — an internet connection is needed for lookups.
- Your collection is in `library.db` (SQLite). **Back up by copying that file.**
- Find → *Export CSV* gives a spreadsheet of everything with locations.
- New sets released? Binders & boxes → *Refresh set list*.
