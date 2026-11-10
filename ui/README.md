# Bifrost UI

React, TypeScript and Vite frontend for Bifrost.

```bash
npm install
npm run dev        # dev server on http://localhost:5173
npm run build      # typecheck (tsc -b) and production build
npx vitest run     # unit tests
npm run lint       # eslint
```

The dev server proxies `/api` to `http://localhost:8000`, so start the backend first (`make up` from the repo root).
