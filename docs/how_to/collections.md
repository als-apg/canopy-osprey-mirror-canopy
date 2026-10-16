# Write a collection

A collection is a named, ordered set of screens and tools that the viewer opens
together, optionally arranged in a dock layout. Users keep their own; a
deployment can also ship **facility collections** that every user sees. This
page is for whoever writes the facility ones.

The format and every rule are defined once, in the docstring of
`canopy_mcp.collections`, reproduced at the [end of this page](#the-format).
The server's `PUT`, the readers of both directories and `--check` all apply the
same function to it, and the viewer applies the same rules again. Where this page
and that text differ, the text is right.

## 1. Where the files live

| Variable | Default | Meaning |
|----------|---------|---------|
| `CANOPY_FACILITY_COLLECTIONS_DIR` | unset | A read-only directory of `<id>.json` facility collections. Unset means none. |
| `CANOPY_COLLECTIONS_DIR` | `<CANOPY_DRAFTS_DIR>/collections` | The user's own collections and their `.state.json`. |

The server never writes the facility directory. The id of a facility collection
is its file stem. A file that cannot be read or breaks a rule is logged once
and left out of the list, so a mistake there does not show up in the viewer:
it simply disappears. Run [`--check`](#4-check-the-directory) before you deploy.

## 2. Members

A member is either a screen or a tool:

```json
{"screen": "library/sr/overview", "macros": {"SECTOR": "3"}}
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
    {"screen": "library/gtb/GTLView"},
    {"screen": "library/bts/BTSView"},
    {
      "tool": "trace",
      "name": "Gun high voltage and heater",
      "query": "pv=EG______HV_____AM00&pv=EG______HTR____AM00&range=-1h"
    }
  ]
}
```

`id` may be left out (the stem is the id); if present it must equal the stem.
`updated` may be left out too, and the file's modification time stands in.

## 3. Slots and the layout

A `layout` places members in a dock grid when the collection is opened. A
member takes part by carrying a `slot`, a short id
unique within the collection, and the layout places slots:

```json
{
  "name": "Ring grid",
  "members": [
    {"screen": "library/sr/overview", "slot": "a"},
    {"screen": "library/s3-promo/promotion", "slot": "b"},
    {"tool": "trace", "name": "Beam current", "query": "pv=SR:DIAG:DCCT:01:CURRENT:RB&range=-1h", "slot": "c"},
    {"tool": "correlation", "name": "Correlation", "slot": "d"}
  ],
  "layout": {
    "split": "row",
    "items": [
      {"items": [{"slot": "a"}, {"slot": "c"}], "size": 2},
      {"items": [{"slot": "b"}, {"slot": "d"}], "size": 1}
    ],
    "active": "a"
  }
}
```

- Only the root names `split` (`row` or `column`). Each nested level turns the
  other way, so the example is two columns side by side, the left one twice as
  wide, each split top and bottom.
- Every split holds at least two items, and splits nest at most 6 deep counting
  the root.
- `size` is a relative weight, any positive finite number.
- Each leaf names a member's slot, each slot at most once. `active` names the
  slot that has focus after opening.
- A member with no leaf, slotted or not, is still opened, undocked.

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
python -I -m canopy_mcp.collections /srv/canopy/collections --check --library /srv/canopy/library
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
5. Run [`--check`](#4-check-the-directory) with `--library`.

`updated` can stay. The user's own copy is untouched; delete it if it should
not appear twice.

## The format

::: canopy_mcp.collections
    options:
      show_root_heading: false
      show_root_toc_entry: false
      show_source: false
      members: false
