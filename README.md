# Corp Dev AI Talent Pipeline Dashboard (Prototype)

First interactive scaffold for tracking corp dev interactions across AI talent acqui-hire opportunities.

## Features in this version
- Left navigation with `Global View`, `Present Targets`, and `Past Deals`
- Search across project names/themes/stage
- Add New Project modal (company name, URL, deal lead, integration lead, codename)
- Codename helper with three one-word suggestions based on company name (manual override supported)
- Global portfolio snapshot metrics and talent-yield theme chart
- Per-project workspace:
  - Company overview and interaction timeline
  - Editable overview cards (status, priority, founded, HQ, team size, codename, valuation, total funding)
  - Auto-filled short company description + product offering highlights (deduped/compacted)
  - Talent roster with drag/drop CSV/TSV import (semantic header mapping)
  - Project Documents section with folder creation, drag/drop upload, and view/download actions
  - Valuation Calculator with scenario simulation and comps benchmarking
  - Right-side document viewer for selected files

## Run locally
```bash
cd /Users/maneet/Documents/Playground/corp-dev-dashboard
python3 server.py
```
Open: `http://localhost:8610/static/index.html`

## Notes
- Current data is seeded in `static/index.html` for rapid prototyping.
- PDF uploads are local to your browser session (not yet persisted).
- Document links are placeholders (`#`) right now.
- Company research uses live website metadata plus DuckDuckGo search snippets at runtime.

## Next build inputs to provide
1. Required project fields (status taxonomy, stage definitions, owner model, etc.)
2. Talent roster schema (comp expectations, relocation, visa, retention risk, etc.)
3. Document categories and access rules
4. Whether you want local file persistence or integration with your internal data/document systems
