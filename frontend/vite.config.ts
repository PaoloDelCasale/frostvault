import path from "node:path";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

const root = path.dirname(fileURLToPath(import.meta.url));

// Proxy /api, /auth and /login to uvicorn. changeOrigin rewrites Host to the
// backend so FastAPI's ALLOWED_HOSTS check (ADR-0005) accepts the request —
// without it the Host is the Vite origin (e.g. localhost:5173) and login fails
// with "Host not allowed" when ALLOWED_HOSTS is set.
const backend = "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      registerType: "autoUpdate",
      includeAssets: ["pwa-192.png", "pwa-512.png"],
      manifest: {
        name: "FrostVault",
        short_name: "FrostVault",
        description: "Self-hosted S3 archival and recovery",
        start_url: "/",
        display: "standalone",
        background_color: "#f4f7f4",
        theme_color: "#257a4b",
        icons: [
          {
            src: "pwa-192.png",
            sizes: "192x192",
            type: "image/png",
            purpose: "any",
          },
          {
            src: "pwa-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "any",
          },
        ],
      },
      injectManifest: {
        globPatterns: ["**/*.{js,css,html,png,svg,ico,webmanifest}"],
      },
      devOptions: {
        enabled: false,
      },
    }),
  ],
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
