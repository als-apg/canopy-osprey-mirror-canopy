# Write a collection

A collection is a named, ordered set of screens and tools that the viewer opens
together, optionally arranged in a dock layout of tiles with tabs. Users keep their own; a
deployment can also ship **facility collections** that every user sees. This
page is for whoever writes the facility ones.

The format and every rule are defined once, in `src/canopy_mcp/COLLECTIONS.md`
beside the code. The server's `PUT`, the readers of both directories and
`canopy-collections-check` all apply the same function to it, and the viewer
applies the same rules again. Where this page and that text differ, the text
is right.

## 1. Where the files live

| Variable | Default | Meaning |
|----------|---------|---------|
| `CANOPY_FACILITY_COLLECTIONS_DIR` | unset | A read-only directory of `<id>.json` facility collections. Unset means none. |
| `CANOPY_COLLECTIONS_DIR` | `<CANOPY_DRAFTS_DIR>/collections` | The user's own collections and their `.state.json`. |

The server never writes the facility directory. The id of a facility collection
is its file stem. A file that cannot be read or breaks a rule is logged once
and left out of the list, so a mistake there does not show up in the viewer:
it simply disappears. Run [the check](#4-check-the-directory) before you deploy.

## 2. Members

A member is either a screen or a tool:

```json
{"screen": "library/facility/overview", "macros": {"SECTOR": "3"}}
{"tool": "trace", "name": "Beam current", "query": "pv=SR:DIAG:DCCT:01:CURRENT:RB&range=-1h"}
```

- A facility collection may name `library/<id>` screens only. A personal one
  may also name the user's `drafts/<id>`.
- `tool` is `trace`, `correlation` or `waveform`. A tool member of a facility
  collection must carry a `name`; that is its label in the viewer. A missing
  `query` opens the tool with the deployment's default query, `""` opens it
  blank. The query is the tool's own URL query and is not parsed by the server.
- No member may appear twice. Two screen members are the same when their
  screen and macros are (the viewer's own macros such as `theme` do not count);
  two tool members when their tool and query are. `name` never counts.

Here is a complete facility collection, `injector.json`:

```json
{
  "name": "Injector",
  "description": "Gun-to-linac and booster-to-storage-ring transfer lines.",
  "members": [
    {"screen": "library/injector/gun"},
    {"screen": "library/injector/transfer-line"},
    {
      "tool": "trace",
      "name": "Gun high voltage and heater",
      "query": "pv=GUN:HV:VOLTAGE&pv=GUN:HTR:CURRENT&range=-1h"
    }
  ]
}
```

`id` may be left out (the stem is the id); if present it must equal the stem.
`updated` may be left out too, and the file's modification time stands in.

## 3. The layout

A `layout` arranges the members in the dock when the collection is opened:
which tiles there are, which members sit in each tile as tabs, which tab
each tile shows, and how big the tiles are. Members are named by their
0-based index in `members`:

```json
{
  "name": "Ring grid",
  "members": [
    {"screen": "library/facility/overview"},
    {"screen": "library/facility/overview", "macros": {"SECTOR": "3"}},
    {"tool": "trace", "name": "Beam current", "query": "pv=SR:DIAG:DCCT:01:CURRENT:RB&range=-1h"},
    {"tool": "correlation", "name": "Correlation"}
  ],
  "layout": {
    "split": "row",
    "items": [
      {"tabs": [0, 1], "showing": 1, "size": 2},
      {
        "split": "column",
        "items": [{"tabs": [2]}, {"tabs": [3]}]
      }
    ]
  }
}
```

This opens two tiles side by side, the left one twice as wide. The left tile
holds both overview screens as tabs and shows the sector 3 one; the right
side is split top and bottom into the Trace and the Correlation.

- A tile is `{"tabs": [...], "showing"?: ..., "size"?: ...}`. `tabs` lists
  at least one member index, in the order of the tile's tab bar; `showing`
  is one of them and defaults to the first.
- A split is `{"split": "row" | "column", "items": [...], "size"?: ...}`
  with at least two items. A split may hold splits of either direction,
  nested as deep as you need; only the size of the layout (32 KiB) is
  bounded.
- `size` is a relative weight among siblings, any positive finite number; a
  missing weight counts 1.
- The whole layout may be a single tile: `{"tabs": [0, 1, 2]}` opens every
  member as a tab of one tile.
- Every member is placed in exactly one tile, exactly once.
- A tile carries no id: the viewer mints tile ids when it opens the
  collection.

A collection without a `layout` is a plain set of members: a click in the
sidebar opens them side by side, one tile each (a near-square grid when
they do not fit in one row, or tabs of one tile with a notice when even
that does not fit), and an agent chooses at open time whether they open as
tabs, side by side or in a layout it gives.

Writing a layout by hand is rarely needed: arrange the screens in the viewer,
save the open tabs as a personal collection with the layout included, and
[promote the file](#5-promote-a-personal-collection).

### `default`

```json
"default": true
```

Legal only in a facility collection; the personal store refuses it. A
collection with `default: true` is shown to every user who has not unticked
it. Leave it out, or set `false`, and users tick the collection themselves.

## 4. Check the directory

```bash
canopy-collections-check /srv/canopy/collections --library /srv/canopy/library
```

Every `*.json` directly in the directory is checked exactly as the server
would read it. With `--library`, every `library/<id>` member must also be a
screen that library lists. Each refused file is one line on stderr, then a
summary on stdout:

```
canopy_mcp.collections: refused: injector.json: members[2]: every tool member of a facility collection carries a name
collections: 4 checked, 1 refused
```

The exit status is `0` when nothing is refused (a missing directory checks
nothing), `3` on any refusal and `1` when the directory or the library cannot
be read. Nothing is written, so the check runs from a read-only tree.

## 5. Promote a personal collection

A personal collection is stored as `<CANOPY_COLLECTIONS_DIR>/<id>.json` in the
same format. Promoting it is copying that file:

```bash
cp ~/data/canopy/collections/ring-grid.json /srv/canopy/collections/ring-grid.json
```

Then, before deploying:

1. Make sure the file name is the id you want. If you rename the file, change
   or delete its `id` field to match.
2. Replace any `drafts/<id>` screen by its library screen; ask for the draft to
   be added to the library with
   [`request_push`](request_push.md) first if it is not there yet.
3. Give every tool member a `name`.
4. Add `"default": true` if every user should see it.
5. Run [the check](#4-check-the-directory) with `--library`.

`updated` can stay. The user's own copy is untouched; delete it if it should
not appear twice.

## The format

The full format, every rule and the HTTP surface are in
`src/canopy_mcp/COLLECTIONS.md` in the Canopy repository.
