# Ask for a screen to be added to the library

A user's screens live in a personal store that only that user's panel-builder
server writes to. The shared library is read-only to every server. The one
way from the first to the second is the `request_push` tool: it files a
request, and an administrator reviews it on the host. **Nothing is published
by the call.**

This page describes the tool and the bundle it writes. How a bundle is
reviewed, accepted and committed is the host's business and is described in
the host's own documentation.

## 1. Give the server a push directory

```bash
CANOPY_PUSH_DIR=/data/canopy/push
```

`request_push` writes one directory per request below it. The directory has
to be writable by the server and readable by whoever reviews. Without
`CANOPY_PUSH_DIR`, or with one that cannot be written, the tool is still
offered and answers `push_unavailable`.

`request_push` is an authoring tool. With `CANOPY_AUTHORING=0` it is not
offered at all. Both switches are listed in the `canopy_mcp` package README.

## 2. Call the tool

```
request_push(id, target_dir, note, source_dir=None)
```

| Argument | Meaning |
|----------|---------|
| `id` | The draft to share. A `library/...` screen cannot be pushed. |
| `target_dir` | The library directory the files should land in, in id form (`sr/rf`). It may not start with `library` or hold a reserved name. |
| `note` | Plain text for the reviewer: what the screen is and why it belongs in the library. At most 2000 characters. |
| `source_dir` | The directory of the user's drafts that is copied. It must contain `id` and everything `id` refers to. Default: the directory of `id`. |

The request holds a copy of the screen, of every screen it embeds or opens,
and of every stored image they show. In the library the files would land at
`screens/<target_dir>/<path below source_dir>`.

The answer is

```json
{
  "draftId": "rf/overview",
  "rid": "3f9c0a7d5e1b2c48",
  "rev": "<sha256 of the screen>",
  "sourceDir": "rf",
  "targetDir": "sr/rf",
  "files": ["overview.screen.json", "img/cavity.png"]
}
```

`rid` names the request. The agent gives it to the user, because the reviewer
needs it.

## 3. Review the bundle

```
<CANOPY_PUSH_DIR>/<rid>/
├── files/<path below source_dir>   one per member
└── request.json
```

`request.json` describes the request:

```json
{
  "format": "canopy-push/1",
  "rid": "3f9c0a7d5e1b2c48",
  "source_id": "rf/overview",
  "source_dir": "rf",
  "rev": "<sha256 of the top screen's bytes>",
  "target_dir": "sr/rf",
  "note": "RF overview for the control room wall.",
  "created": "2026-01-31T12:00:00Z",
  "files": [{"path": "overview.screen.json", "size": 1834, "sha256": "..."}]
}
```

`files` is sorted by path. `request.json` is written last, through a
temporary file and a rename: a request directory without it is an interrupted
write, and the next push deletes it.

The bundle is cut from one consistent state of the store. A draft the user
saves while the push runs is neither blocked nor read, and what is checked is
exactly what is written.

A reviewer reads the screens for what they can do before accepting them.
[`python -m canopy_mcp.report`](library_report.md) lists every PV a screen
tree can write, every command it can run and every URL it loads. A host's
review tooling runs it over the library as it would be with the request
accepted, and the report written at acceptance stays with the library.

!!! warning "Review the images too"
    Once a request is accepted, every user's browser decodes its images.
    The tool judges a pushed image by its suffix and its size; it does not
    decode it.

## The review queue

Every request in the push directory waits for review until the reviewer
removes it. A push never deletes one. Once 8 requests are waiting, the next
push is refused with `push_queue_full` until the reviewer has worked through
some of them.

## Refusals

A refusal is returned, not raised, and files nothing:

```json
{"draftId": "rf/overview", "refused": true, "reason": "check_errors", "message": "..."}
```

| `reason` | Meaning | Extra key |
|----------|---------|-----------|
| `push_unavailable` | No push directory is configured, or it cannot be written. | |
| `push_queue_full` | 8 requests already wait for review. Push again once the reviewer has worked through some. | |
| `read_only_store`, `library_source` | `id` names a library screen. A push comes from the user's own drafts. | |
| `closure_refused` | A reference or rule would not mean the same in the library: an unresolved reference, a macro in a reference, a reference that leaves `source_dir`, a root-relative or cross-root reference, a rule that retargets a reference or a PV channel. | `refusals` |
| `grammar_refused` | A name or content the library does not take (see the limits below). | `refusals`: `{code, path, message}` |
| `check_errors` | A pushed screen has error findings of `check`. Fix them and push again. | `errors`, each with its `screen` |

Bad arguments (an invalid id, `source_dir`, `target_dir` or `note`, or an id
with no stored draft) are a tool error instead.

!!! note "A warning does not refuse a push"
    Only error findings refuse. A `spec_unavailable` warning, which `check`
    returns while no specification is staged, lets the push through with the
    schema and property checks not run. The reviewer should know that those
    two checks did not happen.

## Limits

| Rule | `code` |
|------|--------|
| A screen id is letters, digits, `_` and `-` in `/`-separated segments; an image path segment may also hold spaces and dots. | `bad_segment` |
| No segment is a reserved name (such as `__pycache__`, or a name the deployment lists in `CANOPY_LIBRARY_IGNORED_NAMES`), starts with `.` or `-`, or ends in a suffix such as `.log` or `.pyc`. | `reserved_segment` |
| An image is a `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp` or `.ico` file. SVG is refused, since a browser executes it. | `suffix` |
| At most 5 MiB per file. | `file_too_large` |
| At most 256 files. | `too_many_files` |
| At most 32 MiB in total. | `bundle_too_large` |
| No string of a pushed screen holds a control, format or bidirectional character; `pointer` says where. | `invalid_text` |
| No two paths, and no two directory prefixes, differ only in case; `paths` lists them. | `case_collision` |
| The file list of `request.json` stays under 64 KiB. | `request_too_large` |
