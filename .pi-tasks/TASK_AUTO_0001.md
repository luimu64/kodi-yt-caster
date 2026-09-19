---
id: TASK_AUTO_0001
state: in_progress
phase: done
created_at: 2026-09-19T07:35:57.678Z
updated_at: 2026-09-19T09:04:55.707Z
title: Implement refactor described within docs/STATE-TRANSFER-REFACTOR.md
---

## feature prompt

Implement refactor described within docs/STATE-TRANSFER-REFACTOR.md

## clarifications

(none)

## tasks

- [x] P01 TASK_0001 a1  Establish SessionState as the single in-process state owner — extract an immutable snapshot data class
- [x] P02 TASK_0002 a1  Implement event reduction for all state changes — pure idempotent reducer and reduction logging
- [x] P03 TASK_0003 a1  Convert player and window management to derived projections — remove timer repair loops
- [ ] P04  Publish outbound state batches by snapshot version — diff against last published state with monotonic ofs
- [ ] P05  Support multiple channel subscribers on a single model — independent ofs sequences and sockets
- [ ] P06  Persist the durable session record — save playlist, position, and playback cpn across restarts
- [ ] P07  Derive currentIndex at publication time — compute index from stored queue instead of carrying field
- [ ] P08  Track signal confidence and allow publishing UNKNOWN — retire stall heuristics
- [ ] P09  Reconcile player state once per tick through the reducer — emit corrective events on drift
- [ ] P10  Declare event vocabulary and implement ad and up-next events — log coverage gaps
- [ ] P11  Implement the bidirectional reliability matrix acceptance test — verify phone and Kodi convergence under soak

## coverage

1 grounded requirement(s): 0 task-mapped, 1 cross-cutting (carried into every task via .pi-tasks/requirements.md), 0 unowned
- carried: "Implement refactor described within docs/STATE-TRANSFER-REFACTOR.md"
