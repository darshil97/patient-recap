import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,

  // The old Vite app still lives in legacy-vite/ for reference. It has its own
  // package.json and node_modules; keep the Next compiler away from it.
  outputFileTracingExcludes: {
    "*": ["./legacy-vite/**", "./design-history/**"],
  },
};

export default nextConfig;
