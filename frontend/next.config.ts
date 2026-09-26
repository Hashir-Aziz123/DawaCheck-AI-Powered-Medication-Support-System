import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Allow the backend base URL to be set via environment variable
  env: {
    NEXT_PUBLIC_API_BASE_URL: process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000",
  },
};

export default nextConfig;
