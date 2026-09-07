import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone output is what the Dockerfile copies; it contains no secrets
  // because the frontend never receives any. NEXT_PUBLIC_* values are the
  // only configuration it sees.
  output: "standalone",
  // The repository sits inside a home directory that has its own stray
  // package-lock.json; pin the workspace root so Turbopack ignores it.
  turbopack: { root: __dirname },
};

export default nextConfig;
