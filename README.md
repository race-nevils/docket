# docket

A keyboard-first review bench for visual evidence. One Python file, stdlib only,
no dependencies, no build step.

Point it at folders of images — detector output, dataset candidates, screenshots,
render diffs, anything a human needs to judge — and work through them at keyboard
speed: **Y** yes · **N** no · **F** flag · **H** hold · **U** undo. Every verdict is
appended to a JSONL log next to a frozen, content-addressed copy of the exact
pixels you judged.

Built for the human-in-the-loop step of ML dataset curation, where the labeling
platforms are heavyweight and the actual job is "look at this, say yes or no,
and never lose the answer."

## Quickstart

```
python3 docket.py demo
```

generates a synthetic detector-output fixture (two decks of clustered shape
candidates with a few planted mismatches, plus a bare folder of images) and
serves it at `http://127.0.0.1:8017/`. Judge away — a couple of the clusters
contain an intruder shape, so the demo review has real calls in it.

To review your own images with zero setup:

```
python3 docket.py serve ~/some/folder/of/images
```

Any folder containing images becomes a deck; every image becomes a card.

## Decks and cards

A **deck** is a directory. Two kinds:

- **Loose deck** — a directory of images with no sidecar. One card per image.
- **Described deck** — a directory containing a `cards.json` sidecar. You control
  the cards: labels, summaries, groups of images, links to source documents.

`cards.json`:

```json
{
  "title": "Anchor bolts — batch 12",
  "cards": [
    {
      "key": "a3f9c2e8b1d40767",
      "label": "cluster 04 — anchor bolt",
      "summary": "**12** instances · ~21px · judge: all really anchor bolts?",
      "images": ["cluster-04/zoom.png", "cluster-04/tiles.png"],
      "links": [
        {"label": "Full sheet", "file": "sheets/p03.png"},
        {"label": "Drawing PDF p3", "file": "drawing.pdf", "page": 3}
      ]
    }
  ]
}
```

Field notes:

- `key` — the card's identity. If it is 16 lowercase hex characters it is used
  verbatim; anything else (or a missing key) is hashed together with the deck
  path. **Make the key a content hash of what the card shows** (as the demo
  does) and the id survives re-emitting the deck: an unchanged card keeps its
  verdict, a genuinely changed card gets a fresh id and shows up as pending
  again. That property is the backbone of an iterate-and-re-review loop.
- `summary` — plain text; `` `code` `` and `**bold**` render.
- `images` — inlined on the card (thumbnailed when [Pillow](https://pypi.org/project/pillow/)
  is installed, full files otherwise; click any image for full resolution).
- `links` — buttons that open files in a new tab. A `page` number on a PDF link
  deep-links into the browser's PDF viewer (`#page=N`), which works because the
  file is served over http.

Sidecars are discovered at any depth under the roots you pass; a described
deck's subtree is not walked further, so per-card asset folders never become
decks of their own. Hidden directories and `*-data` directories are skipped.

## What a verdict writes

All output lands under `--data` (default `./docket-data`), never under your
input roots — the input tree is read-only to this tool.

```
docket-data/
  verdicts.jsonl      # append-only: one line per keystroke, fsync'd
  evidence/           # frozen copies of judged images, named by sha256
  note-media/         # screenshots pasted into notes
  deleted.jsonl       # append-only tombstones
  thumbs/             # cache (safe to delete)
```

A verdict line:

```json
{"id": "a3f9c2e8b1d40767", "verdict": "YES", "note": "", "ts": "2026-08-01T12:34:56.789-05:00",
 "evidence": {"deck": "batch-12", "label": "cluster 04 — anchor bolt",
              "images": [{"rel": "batch-12/cluster-04/zoom.png",
                          "sha256": "…", "file": "….png"}]}}
```

Three guarantees, enforced by construction:

1. **The log is append-only.** State is derived by replaying it (last write per
   id wins; `UNDO` retracts). Nothing rewrites or reorders history.
2. **Evidence is frozen at click time.** The exact bytes of every image the card
   showed are copied into `evidence/` under their sha256 before the verdict
   line lands. If the source images later change or vanish, the record of what
   you actually judged does not.
3. **Delete is a tombstone.** The 🗑 button (two-step confirm) appends the
   card's id to `deleted.jsonl`; the feed filters it everywhere, and a re-emitted
   card with the same content hash stays hidden. To restore, append
   `{"id": "…", "op": "undelete"}` to the same file. No file of yours is touched.

## Flow

- **Tabs / keys 1–5** — pending · flagged · yes · no · hold. Pending keeps deck
  order with deck headers; verdicted tabs sort most-recent-first.
- **Notes** — every verdict pauses for an optional note; paste screenshots
  straight into the note box (they're stored server-side and re-attached on
  reload). `Ctrl+Enter` pins, `Esc` skips — the verdict itself is already saved
  either way.
- **FLAG vs HOLD** — flag means "this needs rework, with my feedback attached";
  hold means "real, but not now." Both keep the card out of pending without
  pretending it was decided.
- The page reloads itself when the server restarts (boot-id poll), so
  regenerating decks is just: restart the server, keep the tab.

## CLI

```
python3 docket.py serve [ROOT ...] [--port 8017] [--host 127.0.0.1] [--data DIR]
python3 docket.py check [ROOT ...]        # parse decks, list cards, exit 1 on warnings
python3 docket.py demo  [--dir DIR] [--port 8017]
```

`check` is CI-friendly: it validates every sidecar, resolves every referenced
file, and reports duplicates and missing images without starting a server.

## Tests

```
python3 -m unittest discover -s tests
```

Stdlib only, like everything else. The suite generates the demo fixture, runs a
real server, and exercises verdicts, evidence freezing, tombstones, traversal
guards, and the note-media round trip.

## Non-goals

Multi-user auth, cloud sync, annotation geometry (boxes/polygons), model
training. This is the judgment step, done well, in one file you can read in a
sitting. The server binds to localhost by default and trusts its operator —
put it behind your own auth if you expose it.

## License

MIT
