import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "url";

/**
 * There were no frontend tests at all, which is why the bugs this suite covers
 * — a UTC "today", a polling loop that outlived its component, a proxy trusting
 * the wrong headers — were found by reading rather than by running.
 *
 * Deliberately narrow: these three modules hold logic rather than markup, and
 * logic is what regresses silently.
 */
export default defineConfig({
  // `as never`: @vitejs/plugin-react is typed against rolldown's plugin context
  // while the installed vite resolves rollup's, and the two PluginContextMeta
  // shapes differ. Purely a types mismatch between build tools — the plugin runs
  // correctly — and the alternative is excluding this file from `tsc`, which
  // would stop checking the parts that are ours.
  plugins: [react() as never],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.{ts,tsx}"],
    globals: true,
  },
});
