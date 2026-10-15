import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,

  // The old Vite app still lives in legacy-vite/ for reference. It has its own
  // package.json and node_modules; keep the Next compiler away from it.
  outputFileTracingExcludes: {
    "*": ["./legacy-vite/**", "./design-history/**"],
  },

  images: {
    // The public home page's photographs come from Unsplash and go through
    // next/image, so the server fetches, resizes and re-encodes them and the
    // browser only ever asks our own origin (/_next/image). That keeps the
    // property the rest of the app has — the browser talks to nobody else — and
    // means visitors' IPs are not handed to a third-party CDN.
    //
    // Narrow on purpose: the optimizer will fetch any URL a pattern allows, so a
    // broad one turns it into an open image proxy. One host, photo paths only.
    remotePatterns: [
      { protocol: "https", hostname: "images.unsplash.com", pathname: "/photo-*" },
    ],
  },
};

export default nextConfig;
