import coreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

/**
 * There was no ESLint config and no `lint` script at all, so nothing caught the
 * unused exports, the stale effect dependencies or the accessibility warnings a
 * stock Next setup reports for free — several of which turned out to be real.
 *
 * eslint-config-next 16 ships native flat configs, so these are spread directly
 * rather than wrapped in FlatCompat (which fails on this version).
 */
const config = [
  ...coreWebVitals,
  ...nextTypescript,
  {
    // Not ours and not built. The previous Vite SPA is kept for reference only
    // (§5.2) and is already excluded from the Next build and typecheck; linting
    // it would report on code nobody is going to change.
    //
    // lib/types/api.d.ts is generated from /openapi.json by `npm run gen:api`.
    // Hand-editing it is the thing we are trying to prevent, so linting it is
    // noise about code that gets overwritten.
    ignores: [
      "legacy-vite/**",
      "design-history/**",
      ".next/**",
      "node_modules/**",
      "lib/types/api.d.ts",
    ],
  },
  {
    rules: {
      /**
       * Downgraded from error to warning, deliberately, with an expiry date on
       * the reasoning rather than a shrug.
       *
       * This rule is new in eslint-plugin-react-hooks 7 and it is right: calling
       * setState synchronously in an effect causes a second render pass. It
       * fires seven times here, and every one is the same shape — "fetch on
       * mount, setState with the result", which is how all six views load their
       * data.
       *
       * Silencing it per-line would hide the pattern; fixing it properly means
       * moving data loading to Suspense with `use()`, or adopting a query
       * library. That is a real change to every view and it is not a change to
       * make in passing, so it is a warning: visible in the output, not blocking
       * the build, and not pretending to be resolved.
       */
      "react-hooks/set-state-in-effect": "warn",
    },
  },
];

export default config;
