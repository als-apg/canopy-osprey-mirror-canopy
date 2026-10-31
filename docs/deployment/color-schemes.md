# Color schemes

Converted screens do not hard-code their named colors. Where the source panel
used a named color (a Phoebus named color, or an EDM `colors.list` entry), the
converted widget carries a **color role** instead, and the viewer paints the role
from the active **color scheme** in light or dark mode.

Every deployment ships the built-in scheme `canopy-default`. Its source is
`src/canopy/core/data/canopy-default.json`; the PyDM converter package carries a
byte copy of it. A facility can layer
its own scheme file on top of it to change any role, add roles, or change how
source color names map onto roles.

## Color roles

A colored property holds either a plain hex string or a role object:

```json
{ "$role": "MAJOR", "fallback": "#da1e28", "fallbackDark": "#fa4d56" }
```

| Key | Required | Meaning |
|-----|----------|---------|
| `$role` | yes | Role name, 1-64 characters |
| `fallback` | yes | Hex color (`#rrggbb` or `#rrggbbaa`) used when the scheme has no such role |
| `fallbackDark` | no | Hex color used in dark mode when the scheme has no such role |

The viewer resolves a role in this order: the scheme's value for the role in the
active mode, then `fallbackDark` (dark mode only), then `fallback`. A role the
scheme does not define therefore still paints its fallback.

`canopy-default` defines these roles: `Transparent`, `OK`, `MINOR`, `MAJOR`,
`INVALID`, `DISCONNECTED`, `Text`, `ActiveText`, `Background`,
`Read_Background`, `Write_Background`, `Button_Background`, `Grid`,
`Display_Background`, `On`, `Off`, `Header_Background`, `Header_ForeGround`,
`Attention`, `STOP`.

Related MCP tool behaviour:

- `check` reports a role the served scheme does not define as an
  `unknown_role` **warning**, never an error.
- `patch_node` accepts a role object as a property value; it replaces the
  target value as a whole.
- A draft that contains a well-formed role is stamped `schemaVersion` `1.1`
  (otherwise `1.0`).
- `screenshot` takes `theme` (`light` or `dark`) to render in that mode.

## The scheme file format

A scheme file is JSON in the `canopy-color-scheme/1` format:

```json
{
  "id": "my-facility",
  "extends": "canopy-default",
  "roles": {
    "OK": "#00a000",
    "Display_Background": { "light": "#f0f0f0" },
    "Beam_On": { "light": "#ffcc00", "dark": "#e0b000" },
    "Read_Background": "@Background"
  },
  "import": {
    "phoebus": { "Beam On": "Beam_On" },
    "edm": { "invisible": null }
  }
}
```

| Key | Required | Meaning |
|-----|----------|---------|
| `id` | yes | Non-empty scheme name, reported in `meta.id` |
| `extends` | no | Only `canopy-default` is allowed, and it is the default when omitted. There are no deeper chains |
| `roles` | no | Role name (1-64 characters) to role value |
| `import` | no | `phoebus` and/or `edm`: source color name to role name |

No other top-level keys are allowed.

A **role value** is one of:

- a hex color, `#rgb`, `#rrggbb` or `#rrggbbaa`, used for both modes;
- an alias `"@Other"`, meaning "the same color as role `Other`", for both modes;
- an object `{ "light": ..., "dark": ... }` holding a hex or an alias per mode.
  A mode you omit is inherited from `canopy-default`.

Your file is merged onto `canopy-default` **per role and per mode**: roles you do
not mention keep their default colors, and new roles are added. Every role must
end up with both a light and a dark value after inheritance. Hex values are
canonicalised (lower case, `#rgb` expanded, a fully opaque `ff` alpha dropped).

The **import maps** say which role a source color name becomes during
conversion. They merge onto the defaults the same way; mapping a name to `null`
deletes the inherited mapping, and every target must be a role that exists after
the merge.

### Errors reject the whole file

If the file cannot be read, is not valid JSON, fails the format, has a dangling or
cyclic alias, imports onto a role that does not exist, or leaves a role without a
light or dark value, the **whole file is rejected** and `canopy-default` is served
instead. Every problem found is listed in `meta.errors`, each process logs one
`ERROR` line, canopy's `GET /health/summary` lists `color-scheme` in `degraded`,
and every conversion carries one warning naming the first error. Run
[`canopy-scheme check`](#tools) before deploying a file.

## Configuration

| Variable | Read by | Default | Description |
|----------|---------|---------|-------------|
| `CANOPY_THEME_FILE` | canopy, canopy-api | unset (`canopy-default`) | Path to the deployment's scheme file. The canopy server serves it to viewers; the converter uses it unless `CANOPY_CONVERTER__SCHEME_PATH` is set |
| `CANOPY_CONVERTER__SCHEME_PATH` | canopy-api | unset | Scheme file for the converter only. Wins over `CANOPY_THEME_FILE` |
| `CANOPY_CONVERTER__COLOR_DEF_PATH` | canopy-api | unset | Opt-in Phoebus `color.def` the converter resolves named colors from. See [Matching a facility color.def](#matching-a-facility-colordef) |

An empty or whitespace-only value counts as unset. The converter's precedence is
`CANOPY_CONVERTER__SCHEME_PATH`, then `CANOPY_THEME_FILE`, then `canopy-default`.

Point both servers at the **same file**. `convert_legacy` with `overwrite=true`
refuses to overwrite drafts when the converter's scheme (`id` and `sha256`)
disagrees with the scheme the canopy server serves.

```bash
CANOPY_THEME_FILE=/etc/canopy/my-facility-colors.json
```

## Changing a scheme: restart both servers

!!! warning "There is no live reload"
    Both servers read and flatten the scheme file **once, at startup**, and keep
    it for the life of the process. After you edit the file:

    1. Check it: `canopy-scheme check /etc/canopy/my-facility-colors.json`.
    2. Restart **canopy and canopy-api** (and the canopy-api worker, which runs
       conversions from the same image).
    3. Reload every open viewer so it fetches the new `/config.json`.

    Restarting only one server leaves viewers and the converter on different
    schemes.

## Checking what is served

**`GET /config.json`** (canopy) carries `colorScheme`, the flattened scheme this
server serves:

```json
{
  "colorScheme": {
    "roles": ["Transparent", "OK", "..."],
    "light": ["#ffffff00", "#24a148", "..."],
    "dark": ["#16161600", "#42be65", "..."],
    "meta": {
      "id": "my-facility",
      "sha256": "<digest of the file bytes>",
      "flatHash": "<digest of the flat scheme served>",
      "served": "my-facility",
      "errors": []
    }
  }
}
```

`roles`, `light` and `dark` are parallel lists. `meta.served` is
`canopy-default` when the file was rejected, and `meta.errors` lists why, each as
`{ "path", "message" }`. `colorScheme` is omitted when the scheme flattener is not
installed. It describes this server's scheme only, not the converter's.

**`GET /health`** is the same body whether or not the scheme loaded. The
converter's summary on canopy-api reports the scheme the converter uses, and
asks for a read key:

| Route | Healthy | Scheme rejected |
|-------|---------|-----------------|
| `GET /health` on canopy and canopy-api | `{"status": "healthy"}` | `{"status": "healthy"}` |
| `GET /converter/v1/health/summary` on canopy-api | `{"status": "ok", "degraded": []}` | `{"status": "degraded", "degraded": ["color-scheme"]}` |

On canopy, `GET /health/summary` lists `color-scheme` in `degraded` and reports
`"status": "degraded"`. It asks for the access credential.

Conversion responses carry `scheme`: `{ "id", "served", "sha256", "errors" }`,
describing the scheme that conversion used.

## Tools

The `canopy-scheme` command ships with the converter package:

```bash
# Validate a scheme file: prints every error and every role without a dark
# value, exits 1 if there is any, else "<file>: ok (N roles)".
canopy-scheme check my-facility-colors.json

# Count the named colors a .bob corpus uses, per (name, slot, saved RGB),
# and with --scheme say what each becomes.
canopy-scheme survey panels/ --scheme my-facility-colors.json [--corpus-root panels/]

# Write the light value of every name in a color table into a scheme file,
# keeping each role's dark value. Without --color-def the built-in Phoebus
# table is used. --id is required when the target file does not exist yet.
canopy-scheme from-colordef --color-def color.def --extends canopy-default \
    --into my-facility-colors.json --id my-facility
```

`from-colordef` turns `Name = Other` and `alias(Other)` into the light alias
`"@Other"` (`--keep-light-aliases` keeps aliases you wrote by hand). Names in the
file but not in the table are reported as removed and left in place; it never
deletes a role.

## Matching a facility color.def

By default the converter resolves Phoebus named colors from its built-in table,
which keeps the light look of converted screens as it is today.
`CANOPY_CONVERTER__COLOR_DEF_PATH` is an **opt-in** switch for a facility that
wants its own `color.def` instead. To switch:

1. Regenerate the light values from the same file:
   `canopy-scheme from-colordef --color-def <color.def> --extends canopy-default --into <scheme file>`.
2. Set `CANOPY_CONVERTER__COLOR_DEF_PATH=<color.def>` for canopy-api.
3. Check the scheme, restart both servers and reload viewers, as
   [above](#changing-a-scheme-restart-both-servers).
