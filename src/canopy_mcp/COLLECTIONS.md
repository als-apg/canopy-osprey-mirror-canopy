# Collections

A collection is a named, ordered set of things the viewer can open. This page is
the format of a collection file and the one rule set every reader and writer
applies (`canopy_mcp.collections.validate_collection`): the personal store's
`PUT`, the reader of both stores and `canopy-collections-check`. The viewer applies
the same rules.

## The file

A collection is a JSON object:

```text
{
  "id": "<VIEW_ID_PATTERN>",
  "name": "<1..120 characters>",
  "description"?: "<text>",
  "members": [<member>, ...],
  "layout"?: <layout>,
  "default"?: true | false,
  "updated": "<ISO-8601>"
}
```

No other key is allowed, here or in a member or a layout node.

- `id` matches `^[A-Za-z0-9_-]{1,64}$`. A facility collection's id is its file
  stem, so the field may be left out; when present it must equal the stem. A personal
  id is minted once by the client and taken from the URL. The store passes the id it
  expects as `file_id`.
- `name`: a string, stripped, 1 to 120 characters, no control or format
  characters (`canopy_mcp.validate.clean_name`).
- `description`: a string, without lone surrogates.
- `updated`: a string when present; a facility file may leave it out (its mtime
  stands in), the personal store writes it.
- `default`: a boolean, legal only in a facility collection. A collection with
  `default: true` is shown to every user who has not unticked it.

A member is one of two kinds (identity and key grammar: `canopy_mcp.member_key`):

```text
{"screen": "<category>/<id>", "macros"?: {"<name>": "<value>", ...}}
{"tool": "<tool id>", "query"?: "<opaque>", "name"?: "<label>"}
```

- `<category>` is `library` or `drafts`; `<id>` matches
  `[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*`. A facility collection names `library/*`
  screens only; a personal one may also name `drafts/*`.
- macro names and values are strings.
- `<tool id>` is one of `canopy_mcp.validate.TOOL_IDS`: the static tuple, not the
  tools this host serves, so a host with fewer tools never drops a file. A missing
  `query` means the tool's config default, `""` a blank tool; a query is opaque
  text of at most `MAX_QUERY_BYTES` (4096) UTF-8 bytes. Whether it parses is the
  viewer's concern.
- `name` follows the collection `name` rule. Every tool member of a facility
  collection carries one.
- No two members share a `member_key`.

## The layout

A layout arranges the members in the dock. It is the collection form of the dock
layout tree (`canopy_mcp.layout`) that the viewer's dock and the agent tools share:

```text
<layout> = <tile> | <split>
<tile>   = {"tabs": [<member index>, ...], "showing"?: <member index>, "size"?: <weight>}
<split>  = {"split": "row" | "column", "items": [<layout>, <layout>, ...], "size"?: <weight>}
```

- a tile lists its tabs as 0-based indexes into `members`, at least one; `showing`
  is one of them and defaults to `tabs[0]`;
- a split holds at least two items and may nest a split of either direction;
- `size` is a positive finite weight among its siblings (a missing weight counts 1);
  the viewer normalises weights to fractions when it opens the collection;
- the root may be a single tile;
- every member is placed in exactly one tile, exactly once;
- a tile has no `tile` id and no `from`: ids are minted when the collection opens.

There is no depth cap: only `MAX_LAYOUT_BYTES` bounds the tree. A collection without
a `layout` is legal and opens as tabs. A broken layout raises `InvalidLayout`,
whose message is `<where>: <reason>: <text>`; `reason` is one of
`LAYOUT_REASONS`, the same strings the viewer's `validateLayout` returns, and
`layout_refusal` replays the viewer's own `validate` cases, copied into
`tests/canopy_mcp/fixtures/dock-cases`.

## Limits and the library

Limits: at most `MAX_MEMBERS` (50) members, the layout at most `MAX_LAYOUT_BYTES`
(32 KiB) and the whole document at most `MAX_COLLECTION_BYTES` (256 KiB), each
measured as compact UTF-8 JSON.

With `library` (a library root, or its `canopy_mcp.listing.Judged`), every
`library/<id>` member must be a screen the library lists:
`screens/<id>.screen.json` among the listed paths and no refusal naming it or a
directory above it. Images never match, by their suffix.

## Stores

The facility store (`FacilityCollections`) is a read-only directory of
`<id>.json` files: the id is the file stem, a missing `updated` reads as the file's
mtime, and a file that is unreadable or breaks a rule is logged once (until it
changes) and skipped. The
personal store (`CollectionStore`) keeps `<personal dir>/<id>.json` with
the rules of `canopy_mcp.views`: every write is one section under an exclusive
`flock` on the personal directory, each file is replaced atomically, an unwritable
directory raises
`canopy_mcp.store.ReadOnlyStore` (`read_only_store`). A listed collection
carries `source`, `ref` (`<source>:<id>`) and `rev`.

`rev` is the first 16 hex digits of the sha256 of the stored file bytes: it changes
whenever the file does and is never stored itself. A client that echoes a listed
collection back may leave `source`, `ref`, `rev` and `updated` in it; `PUT`
drops them before validation. Every personal write states what it expects, and the
store compares under the lock, so no other writer lands between check and write:

| Request | Outcome | Refusal |
| --- | --- | --- |
| `PUT`, neither header | refused | 428 `precondition_required` |
| `PUT` + `If-None-Match: *` | create only | 412 `exists`: the id is stored |
| `PUT` + `If-Match: <rev>` | replace that rev only | 412 `gone`: id not stored; 412 `stale`: another rev |
| `DELETE` + `If-Match: <rev>` | remove that rev only | 412 `stale`: another rev; 404 `not_found` |
| `DELETE`, no `If-Match` | remove whatever is stored | 404 `not_found` |

The details read "send If-None-Match: * to create or If-Match: <rev> to replace",
"collection '<id>' exists", "collection '<id>' is gone" and "collection '<id>' has
changed". `If-Match` names one or more revs, comma-separated, each bare, quoted
or `W/`; `*` matches any stored rev. When both headers are sent, `If-Match` decides.

The per-user state is `<personal dir>/.state.json` (its stem is outside
`VIEW_ID_PATTERN`, so it is never a collection):

```text
{"ticked": [<ref>, ...], "unticked": [<ref>, ...], "pinned": [<member>, ...], "recent": [<member>, ...]}
```

and changes only through one op at a time, applied read-modify-write under the same
lock (`CollectionStore.apply_op`):

```text
{"op": "tick" | "untick", "ref": "<source>:<id>"}
{"op": "pin" | "unpin" | "recent" | "recent-remove", "member": <member>}
{"op": "recent-many", "members": [<member>, ...]}
{"op": "recent-clear"}
```

`OP_FIELDS` maps each op to the one field it carries besides `op`; an op is
exactly those keys:

| op | field | effect |
| --- | --- | --- |
| `tick` | `ref` | add the ref to `ticked`, drop it from `unticked` |
| `untick` | `ref` | the reverse |
| `pin` | `member` | append a member whose `member_key` is not pinned yet |
| `unpin` | `member` | drop the pinned member with its key |
| `recent` | `member` | move it to the front of `recent`, dropping any other with its key |
| `recent-remove` | `member` | drop the recent member with its key |
| `recent-clear` | (none) | empty `recent` |
| `recent-many` | `members` | put up to `MAX_RECENT` members, in order, at the front of `recent` |

`recent-many` keeps the first of a repeated key and drops the other recent members
with its keys; `recent` and `recent-many` keep the first `MAX_RECENT` (10). All
are idempotent, and an op that changes nothing writes nothing. An unknown op is
400 `invalid_op` "op must be one of tick, untick, pin, unpin, recent, recent-remove,
recent-clear, recent-many". A member here
follows the personal member rules. Bounds: an op is at most `MAX_STATE_OP_BYTES` (8 KiB), a `recent-many`
op at most `MAX_RECENT_MANY_OP_BYTES` (48 KiB), the stored state at most
`MAX_STATE_BYTES` (512 KiB), all as compact UTF-8 JSON; `MAX_PINNED` (50)
pinned members, at most `MAX_REFS` (100) refs per list. A full list first evicts the refs naming no current
collection, then raises `SelectionFull` (409 `selection_full`); a full
`pinned` raises `PinnedFull` (409 `pinned_full`). Deleting a personal
collection prunes `personal:<id>` from both ref lists under the same lock.

## HTTP surface

`build_collection_routes`, every response `Cache-Control: no-cache`:

- `GET /collections` -> 200 `{"collections": [...]}`: facility first, then personal,
  each sorted by case-folded name, then id, each carrying `source`, `ref` and
  `rev`. It answers an
  `ETag` (the quoted sha256 hex of the body) and 304 with no body, keeping `ETag`
  and `Cache-Control`, on a matching `If-None-Match` (weak and list forms match);
  `HEAD` mirrors `GET`
- `GET /collections/{id}` -> 200 the listed personal collection (with `rev`), 404
  `not_found`
- `PUT /collections/{id}` with a JSON collection body and `If-None-Match: *` or
  `If-Match: <rev>` -> 201 created, 200 replaced (body: the listed collection, its
  new `rev`); 428 `precondition_required` without either, 412 `exists` /
  `gone` / `stale` (the table above). A `default` key, or any broken rule, is
  400 `invalid_collection`
- `DELETE /collections/{id}` -> 204, 404 `not_found`; with `If-Match`, 412
  `stale` on another rev
- `GET /collections-state` -> 200 the state document, conditional like
  `GET /collections` (`ETag` = sha256 of the body, 304 on a match)
- `POST /collections-state` with one JSON op -> 200 the new state document; 400
  `invalid_op`, 409 `pinned_full` / `selection_full`, 413 `too_large`

`PUT`, `DELETE` and `POST` act on the personal store only. Every error body is
`{"error": "<code>", "detail"?: "<text>"}`; besides the codes above: 400
`invalid_id` (also an id with a slash), 400 `invalid_json`, 403 `read_only_store`
(the personal directory may not be written; the detail is fixed text, never a server
path), 403 `cross_site` (a write whose `Sec-Fetch-Site` is present and is neither
`same-origin` nor `none`; reads are never guarded), 405 `method_not_allowed`
(with `Allow`), 413 `too_large` over `MAX_PUT_BODY_BYTES` /
`MAX_POST_BODY_BYTES`, 415 `unsupported_media_type` unless `Content-Type:
application/json`. As for `canopy_mcp.views`, the routes carry no auth of their
own: they sit behind the app's guard (see *Single-user host or access token* in
the package README).

## Checking a facility directory

```sh
canopy-collections-check <dir> [--library <root>]
```

checks a facility collection directory the way the server would read it and
refuses what the server would silently skip (`canopy_mcp.collections_cli`).
