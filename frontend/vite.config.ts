import path from "node:path";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const root = path.dirname(fileURLToPath(import.meta.url));

// Proxy /api, /auth and /login to uvicorn. changeOrigin rewrites Host to the
// backend so FastAPI's ALLOWED_HOSTS check (ADR-0005) accepts the request —
// without it the Host is the Vite origin (e.g. localhost:5173) and login fails
// with "Host not allowed" when ALLOWED_HOSTS is set.
const backend = "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(root, "./src"),
    },
  },
  server: {
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/auth": { target: backend, changeOrigin: true },
      "/login": { target: backend, changeOrigin: true },
    },
  },
});
