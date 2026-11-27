import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development `/api` goes to the mock server (mock/server.mjs); set
// API_TARGET to point it at a real backend instead. `/otlp` goes to a
// collector on :4318 (OTLP_TARGET) if there is one.
const api = process.env.API_TARGET ?? `http://localhost:${process.env.MOCK_PORT ?? 8787}`;

export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.PORT ?? 5173),
    proxy: {
      "/api": { target: api, changeOrigin: true },
      // Browser telemetry; harmless when no collector is listening.
      "/otlp": { target: process.env.OTLP_TARGET ?? "http://localhost:4318", changeOrigin: true, rewrite: (p) => p.replace(/^\/otlp/, "") },
    },
  },
  build: { sourcemap: false },
});
