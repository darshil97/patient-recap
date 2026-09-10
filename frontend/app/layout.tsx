import type { Metadata, Viewport } from "next";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "Consultation Insights",
  description:
    "Doctor-facing record of what was said during consultations. Does not diagnose.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    // suppressHydrationWarning is required, not a workaround for our own bug.
    //
    // @neondatabase/auth-ui bundles next-themes, which injects a blocking inline
    // script that sets class="dark" and style="color-scheme: dark" on <html>
    // before React hydrates. That is the whole point of it: the theme has to be
    // applied before first paint or the page flashes light then snaps to dark.
    //
    // React hydrates by comparing its client tree against the DOM as it finds
    // it, and by then the script has already run — so those two attributes look
    // like a server/client mismatch and it logs a console error. Nothing is
    // broken; React explicitly supports opting out for exactly this case.
    //
    // Scope is deliberately narrow: this suppresses attribute and text
    // differences on THIS element only, one level deep. It does not silence
    // mismatches anywhere else in the tree, so a real hydration bug in a page or
    // component still surfaces.
    <html lang="en" suppressHydrationWarning>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
