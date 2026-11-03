# Report what a screen library can do

A screen can write PVs, run commands on the host, open other screens and load
content by URL. A shared screen library therefore keeps a **report** beside
its screens: two files that list everything the screens of the tree can do,
for a reviewer to read before the tree is published.

```bash
python -m canopy_mcp.report <root> [--served-at personal|library] (--emit|--write|--check)
```

`<root>` is a store root, the directory that holds `screens/`. The command
needs the Canopy wheel and nothing else; it talks to no server.

## 1. Write the report

```bash
python -m canopy_mcp.report /srv/canopy/library --write
```

This writes two files beside `screens/`:

| File | Content |
|------|---------|
| `LIBRARY-REPORT.json` | The report as one JSON document (schema `canopy-library-report/1`). |
| `LIBRARY-REPORT.md` | The same rows as tables, for reading in a review. |

Commit both together with the screens they describe. Do not edit them by
hand: the check below compares them with a fresh computation.

A `provenance` object already present in the JSON file is carried over
unchanged. The command never writes one and never compares it, so a host may
keep its own review notes there.

## 2. Check that it is current

```bash
python -m canopy_mcp.report /srv/canopy/library --check
```

`--check` recomputes the report and compares it with the stored files: the
JSON without `provenance` and without the Canopy version that generated it,
the markdown byte for byte.

| Exit status | Meaning |
|-------------|---------|
| `0` | The report is current. Also: the root does not exist (`no library`), or it holds no files and no report (`empty library, no report`). |
| `1` | A file is missing, unreadable or differs. Each difference is printed as `stale: ...`, followed by the command that regenerates the report. |
| `3` | The tree is refused (see below). Each reason is printed as `refused: ...`. |

Run the check wherever a library is built into an image or published, and
after every Canopy upgrade: a new version can read more out of the same
screens, and a report that no longer matches has to be reviewed again before
it is regenerated.

## 3. Print without writing

```bash
python -m canopy_mcp.report /srv/canopy/library --emit
```

`--emit` prints one JSON object `{"json": <report>, "md": <markdown>}` on
standard output and writes nothing. Use it to look at a tree that is not
yours to write to, such as a library with a
[push request](request_push.md) under review.

## Where the tree is served

`--served-at` says where the viewer serves the root and defaults to
`library`. It decides how references that start at the root are judged: in a
library they are listed and refused by `--check`, in a personal store they
are only a warning of the `check` tool. Pass `--served-at personal` for a
user's own store.

A library served by the panel-builder server is named by `CANOPY_LIBRARY_DIR`
(see [appliance mode](../deployment/appliance.md)). The server serves only
its `screens/` subtree, so the two report files are never reachable from a
browser.

## What the report lists

| Key | Rows |
|-----|------|
| `generator` | The schema and the Canopy version. |
| `files` | Every file of the tree, with a digest over the listing. |
| `manifest` | The ids of the top-level screens. |
| `templates` | Every screen file with its sha256, its macros and its role: `manifest`, `embedded` or `unlisted`. |
| `screens` | Each top-level screen expanded as the viewer mounts it: its instances, the macros a URL can set, and the references that did not resolve. |
| `writeBindings` | Every PV a screen can write. |
| `executeActions`, `commandButtons` | Every action that runs a command, and every button that can hold one. |
| `openDisplays` | Every action that opens another screen. |
| `absoluteUrls` | Every web page a screen opens, every image fetched by URL, and every other `http://` or `https://` string of a node. |
| `crossRootRefs`, `rootRelativeRefs` | References that leave the tree or start at its root. |
| `ruleRetargetedChannels`, `ruleRetargetedRefs` | Rules that change which PV or which file a widget points at. |

Hidden widgets count everywhere, because a rule can show a widget that is
hidden in the file. Their rows say `hidden`.

## Refusals

Every mode refuses a screen that holds a control, format or bidirectional
character in any string, with `invalid_text: <file> <json pointer>`.

`--check` also refuses:

- an entry that does not belong in a library: anything beside `screens/`
  other than the two report files, a symbolic link, a dotfile or a leftover
  temporary file, a file that is neither a screen nor an image, or names
  that differ only in case;
- any `.svg` file;
- a non-empty `crossRootRefs`, `rootRelativeRefs`, `ruleRetargetedChannels`
  or `ruleRetargetedRefs`.

`--emit` and `--write` print these as warnings, so a reviewer can see the
full report of a tree that would not pass.
