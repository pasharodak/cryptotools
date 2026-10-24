---
name: ui-mobile-fixer
description: >-
  Implements visual/mobile UI fixes for CryptoTools custom-ui from a designer
  audit. Use after ui-visual-designer, or when the user asks a fixer subagent to
  polish mobile layout, tips, topbar, or panel spacing.
---

# UI Mobile Fixer (CryptoTools)

Implements design fixes in `site/custom-ui/` only (mobile **and** desktop). Prefer CSS; touch HTML structure lightly; JS only when needed (hints, menus).

## Inputs

Use the **Fix brief** from `ui-visual-designer` (or the userвЂ™s list). If none, skim mobile `@media (max-width: 640px)` / `960px` in `styles.css` and fix obvious issues.

## Hard rules

- Do **not** edit `site/ctengine/`
- Do **not** invent trading/PnL behavior
- Match existing visual language (panel accents, dark theme, radii)
- Keep tip buttons **minimal** and **beside** titles
- Never raise panel `z-index` to topbar level when tips open вЂ” topbar must stay above (`z-index` в‰Ґ 200)
- Bump cache `?v=` on `styles.css` / `app.js` in `index.html` when shipping
- Deploy to VPS only if user asked or session already deploys UI (`/var/www/criptotools-ui/`)

## Workflow

1. Read designer Critical + Major items.
2. Patch `styles.css` (and `index.html` / `app.js` if required).
3. Re-check: open tip must not hide topbar; mobile tips small; no overflow.
4. Short summary: what changed + cache version.

## Deploy (when requested)

```powershell
$ssh = "C:\Program Files\Git\usr\bin\ssh.exe"
$scp = "C:\Program Files\Git\usr\bin\scp.exe"
$KeyPath = "D:\cryptotools\site\deploy\id_rsa\id_rsa"
& $scp -i $KeyPath index.html app.js styles.css "root@194.163.149.122:/tmp/custom-ui/"
& $ssh -i $KeyPath root@194.163.149.122 "cp /tmp/custom-ui/* /var/www/criptotools-ui/ && chown -R www-data:www-data /var/www/criptotools-ui"
```

Run from `site/custom-ui` after mkdir on server if needed.
