# Project Canopy — Agent Handoff

> **Purpose of this doc:** Bring a fresh Claude agent up to speed on Project Canopy with the minimum context needed to be useful on the next turn. Read top to bottom; the "Open Gaps" section is what the user most likely wants help with.

---

## 1. TL;DR

- **Project name:** **Canopy** (renamed; formerly **PyDM 2.0**, also referred to as **Argus**).
- **Domain:** Control-system UI / operator screens, in the EPICS ecosystem (the same space as PyDM 1.0 and Phoebus).
- **Posture:** Deliberate clean break from PyDM 1.0. Not a port, not a refactor — new architecture. A migration bridge exists so 1.0 screens aren't abandoned, but 2.0/Canopy is its own product.
- **Working artifact:** A "handoff doc" the user authored summarizing the architecture and open gaps. (See §6 — the full doc is **not** in this agent's context; ask the user to paste it if you need details beyond what's summarized here.)

---

## 2. Architecture (as understood from prior chat)

### Core principles
- **Zero-Qt core.** The data plane and services have no Qt dependency. This is a hard rule — it's the thing that distinguishes 2.0 from 1.0 architecturally.
- **Thin frontend.** The UI is a renderer over a typed data stream; logic lives in services, not widgets.
- **Single data primitive.** Frontend components consume PV data through one hook: **`usePV()`**. There is no second blessed path.

### Backend: woodland-named Python services
Five services, each named after a woodland animal:

| Service | Notes (from summary — confirm with user) |
|---|---|
| **Hare** | One of the core services. Specific responsibility not captured in summary. |
| **Wren** | Owns an **expression language** (choice of language is still open). |
| **Mole** | One of the core services. Specific responsibility not captured in summary. |
| **Fox**  | Owns a **formula IR** (intermediate representation for formulas/calcs). |
| **Otter** | One of the core services. Specific responsibility not captured in summary. |

Beneath all of these sits an **EPICS connector** — the actual channel-access / pvAccess layer that talks to IOCs.

> ⚠️ The per-service role mapping (which animal does what) is not preserved in this agent's context. **Ask the user to paste the service-responsibilities section of the handoff doc before doing detailed design work on any individual service.**

### Frontend
- **Stack:** React + Tauri (desktop shell).
- **Design system:** IBM **Carbon** + a layer of **custom control-system widgets** built on top.
- **Data:** Everything PV-shaped flows through `usePV()`. The contract of that hook is one of the explicitly open design areas.

### Migration bridge (1.0 → 2.0)
Three components form the bridge:
1. **`ws://` plugin** — lets 1.0 (Qt) clients speak to 2.0 services over WebSocket.
2. **Runtime renderer** — runs 2.0 screens in some compatibility mode.
3. **One-shot converter** — takes 1.0 screen files and emits 2.0 screens. Its **IR** (intermediate representation) is an open design area.

The bridge is intentionally a bridge — not a long-term compatibility layer.

---

## 3. Phoebus-parity items

Canopy needs to reach feature parity with Phoebus on at least these tools (each is an open piece of design/build work):

- **Probe** — single-PV inspector.
- **PV Table** — tabular multi-PV view.
- **PV Tree** — hierarchical PV browser.
- **Alarm Service** — alarms backend + UI surface.

---

## 4. Known open gaps (⚠️ in the handoff doc)

These are the things flagged as not yet designed / not yet resolved:

1. **Exception handling UX** — how errors surface to operators in screens.
2. **Event dependence / button sequencing** — ordering and dependencies between user-triggered actions.
3. **Help system** — in-app help/docs surface.
4. **Graceful channel close** — clean teardown when PVs/connections go away.

---

## 5. Open design areas (concrete things to work on)

From the last conversation, the user listed these as candidate next steps. Any of them is fair game; let the user pick:

- **Wren's expression language choice** — which language, what semantics.
- **Fox's formula IR** — IR shape, type system, evaluation model.
- **Screen JSON schema** — the wire/disk format for 2.0 screens.
- **`usePV()` contract** — exact API, lifecycle, error surface, subscription semantics.
- **Converter tool IR** — the IR the 1.0→2.0 converter targets.
- Any of the four ⚠️ gaps above.
- Any of the four Phoebus-parity items above.

---

## 6. What this agent does NOT have

Be honest with the user about this so they can fill the gaps:

- **The full text of the original handoff doc.** Prior chats in this project contain a summary written by Claude, but the doc itself the user pasted is not retrievable from search. If detailed design work is needed, ask the user to re-paste the relevant section.
- **Per-service responsibilities for Hare / Mole / Otter** — only Wren (expression language) and Fox (formula IR) had hooks captured in the summary.
- **Any code.** No repository has been shared with this agent. If the user references files or modules, ask for them.
- **Decision history** — i.e., *why* the user chose React+Tauri, Carbon, zero-Qt, woodland naming, etc. Treat these as fixed decisions unless the user reopens them.

---

## 7. How to operate inside this Project

A few practical notes about the environment, learned from prior turns:

- **Memory is project-scoped.** This Project has its own isolated memory. Chats outside the Project (or in other Projects) are *not* searchable from here. If the user references something that should exist but doesn't surface, it's likely in a different scope — say so rather than guessing.
- **The rename is durable.** PyDM 2.0 → Argus → **Canopy**. Use "Canopy" by default; the other names are aliases for older material.
- **Tone the user seems to want.** Direct, technical, no fluff. They're driving an architecture, not learning one — match that register. Don't re-explain EPICS, PVs, Phoebus, IOCs, etc.

---

## 8. Suggested opening move for the next agent

A reasonable first turn after reading this doc:

> "Canopy context loaded — I have the architecture summary (zero-Qt core, Hare/Wren/Mole/Fox/Otter, React+Tauri+Carbon frontend, `usePV()`, ws://+runtime+converter bridge) and the open gaps. The summary is missing per-service responsibilities for Hare/Mole/Otter and the full handoff-doc text. Want to (a) paste those in so I'm fully loaded, or (b) just pick one of the open design areas and dive in?"

That gives the user the choice between topping up context and just getting to work.
