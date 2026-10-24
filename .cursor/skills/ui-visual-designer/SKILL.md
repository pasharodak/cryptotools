---
name: ui-visual-designer
description: >-
  Reviews CryptoTools custom-ui as a product designer (visual QA). Use when the
  user asks for design review, visual audit, mobile polish check, designer
  subagent, or to test how the UI looks before/after fixes.
---

# UI Visual Designer (CryptoTools)

Read-only design QA for `site/custom-ui/` (live panel). Do **not** edit code вЂ” only report findings for the fixer skill / agent.

## Scope

- Primary surface: `site/custom-ui/` (`index.html`, `styles.css`, `app.js`)
- Live URL (prod): `https://194.163.149.122:8443/` (self-signed SSL OK)
- Focus by request: **mobile** (в‰¤640px), **tablet** (в‰¤960px), and/or **desktop** (в‰Ґ1200px). Default order if unspecified: mobile в†’ tablet в†’ desktop.

## How to review

1. Open the live UI in the browser (or inspect local files if offline).
2. Set viewport to **390Г—844** (phone) and **768Г—1024** (tablet).
3. Walk: topbar/menu в†’ panels (Test / Strategy / Grid) в†’ hint `i` buttons в†’ collapsible settings в†’ strategy toggles в†’ stats modal if reachable.
4. Screenshot key states; judge composition, hierarchy, spacing, tap targets, overflow, contrast.

## Checklist

- Hierarchy: titles vs controls; no competing focal points
- Density: no oversized icons/pills; tips sit **next to** title text
- Touch: targets в‰Ґ ~40px where interactive, but tip `i` may stay minimal
- Overflow: no horizontal scroll; tips stay in viewport; no clipping in collapsed sections
- Topbar: sticky menu never hidden by popovers/z-index
- Consistency: gaps, radii, accent colors match existing panel language
- Mobile menu: readable grid, left-aligned, not cramped

## Output format (required)

```markdown
## Designer verdict
One sentence.

## Critical
- [area] problem в†’ expected

## Major
- вЂ¦

## Minor / polish
- вЂ¦

## Fix brief for ui-mobile-fixer
Numbered, concrete CSS/HTML/JS changes (selectors/files). No code dumps unless a 3-line snippet clarifies.
```

Severity: Critical = broken/unusable; Major = looks wrong or awkward; Minor = polish.
