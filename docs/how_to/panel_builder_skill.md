# The panel-builder skill

The panel-builder server (`canopy_mcp`, the FRONT process of
[appliance mode](../deployment/appliance.md)) lets an agent build Canopy
screens through MCP tools. The `canopy-panel-builder` skill is the guide the
agent works by: where screens live, the Screen-IR file format, which tool to
call when, and what to do when a call is refused. This page says how to
install the skill and what the tools are.

## 1. Install the skill

```bash
python -m canopy_mcp.skill install <dir>
```

`<dir>` is the project directory the agent works in. The command writes

```
<dir>/.claude/skills/canopy-panel-builder/SKILL.md
```

and prints that path. Missing directories are created. Running it again is
safe: a copy that already holds the same bytes is left alone, and an older
one is replaced atomically, so a reader never sees half a skill.

The command needs only the standard library. It runs wherever the Canopy
wheel is installed, with or without the `mcp` extra.

## 2. Keep one source of the text

`SKILL.md` ships in the wheel as package data of `canopy_mcp.skill` and is the
single source of the text. Do not edit an installed copy: the next install
replaces it, and a copy that differs from the packaged one describes tools the
server may not have. After a Canopy upgrade, run the install command again.

A host that keeps its own copy can compare it with the packaged bytes:

```python
from pathlib import Path
from canopy_mcp.skill import SKILL_NAME, install, skill_bytes, skill_path

skill_path(Path("project")).read_bytes() == skill_bytes()
```

`SKILL_NAME` is `canopy-panel-builder`, `skill_path(dir)` is where the skill
of a project lives, and `install(dir)` is what the command calls.

The server's MCP `instructions` name the skill and the install command, so an
agent that connects without it is told where to get it.

## 3. Know what the skill covers

| Section | What the agent learns |
|---------|-----------------------|
| Where screens live | The personal store, the read-only library and how ids map to files. |
| The Screen-IR file format | The document, its nodes, macros and references to other files. |
| The tools | The table below, with when to call each. |
| Build a screen | Find widgets and PV names, write, read the findings, look at a screenshot, fix. |
| Edit with `rev` and `base_rev` | Every change names the revision it was made against. |
| Port a legacy panel | `convert_legacy` for `.bob`, `.opi`, `.edl` and `.ui` files. |
| Screenshots and mock mode | What a screenshot shows with live and with mock PVs. |
| Before you commit | Regenerating the [library report](library_report.md). |
| Push to the shared library | [`request_push`](request_push.md) and its refusals. |
| When a call fails | What each refusal means and what to do next. |

## The tools

The server offers 16 tools. Nine only read. Seven are authoring tools: they
create, change or delete the user's own screens, or file one for the library.

| Tool | Kind | What it does |
|------|------|--------------|
| `list_widgets` | read | Lists the widget types a screen may use. |
| `get_widget` | read | Describes one widget type and its props. |
| `get_schema` | read | Returns the Screen-IR JSON Schema. |
| `check` | read | Reports schema, semantic, ref and lint findings for a document or a stored screen. |
| `get_screen` | read | Returns one screen and its rev. |
| `list_drafts` | read | Lists the user's screens, then the library screens. |
| `search_pvs` | read | Searches the PV directory by a glob pattern. |
| `read_pv` | read | Reads one PV once. |
| `screenshot` | read | Renders a screen as a PNG. |
| `new_screen` | authoring | Saves a whole document as a draft. |
| `patch_node` | authoring | Changes one node by JSON Merge Patch. |
| `add_node` | authoring | Appends one node to a container. |
| `remove_node` | authoring | Removes one node and its children. |
| `delete_draft` | authoring | Deletes a draft. |
| `convert_legacy` | authoring | Converts a legacy panel and its sub-screens into drafts. |
| `request_push` | authoring | Files a draft and everything it refers to for an administrator to review. |

No tool writes a PV. Every change to a screen is checked against the rev the
caller last saw, so an agent and a person editing in the viewer cannot
overwrite each other. A library screen (`library/<id>`) can be read, checked
and screenshotted; an authoring tool refuses it with `read_only_store`.

`CANOPY_AUTHORING=0` takes the seven authoring tools off the server. A host
that wants a person to approve each change can instead leave them on and ask
for a confirmation per authoring call in its agent's permission settings: the
nine read tools are safe to allow without one.

!!! note "Checks without a staged specification"
    The widget catalog comes from the viewer build (`ir-map.json`). The
    screen-format schema and the per-widget property schemas come from a
    staged specification directory, which the payload of this release does
    not carry. Until it does, `check` returns one `spec_unavailable` warning
    in place of the schema and property findings, and `list_widgets`,
    `get_widget` and `get_schema` carry a `spec_unavailable` note. See
    [appliance mode](../deployment/appliance.md#spec_unavailable).
