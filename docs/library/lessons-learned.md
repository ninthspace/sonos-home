---
title: Promoted Lessons
source: cpm-next signals promoted by /cpm-next:do
added: 2026-11-05
last-reviewed: 2026-11-05
scope:

  - epics
  - do
  - quick
  - review
summary: >
  Lessons from failures that recurred in this project. When UI output or controls move from one element to another,
  everything the old place provided (ARIA role, styling, headings or labels that gave it meaning) must be carried over
  or replaced, and a criterion must check it.
---

## When output or controls move, carry over what their old place gave them
**Promoted**: 2026-11-05  
**Class**: moved-output-loses-its-semantics  
**Source**: docs/epics/01-10-epic-ui-refinements.md → Story 7 → moved-output-loses-its-semantics, docs/quick/12-quick-phone-room-cards-and-queue-width.md → moved-output-loses-its-semantics  
**Scope**: epics, do, quick, review

When a change moves output or controls from one element to another (a message to another note, rows from a list into cards), the old place often supplied meaning the moved content doesn't carry itself: a status role and error style, a heading that named the group, a label. List what the old container provided before moving anything, and carry each piece over or replace it at the new place. A criterion for the move states those pieces at the new place (for example "the error is announced and styled as an error", or "a grouped room's card names the other rooms in its group"), with a control that shows the first build without them failing. This belongs to the web UI layer (index.html, app.js render functions, style.css).
