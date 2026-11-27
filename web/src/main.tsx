import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import App from "./App";
import { api } from "./api";
import { AuthProvider } from "./auth";
import { startTelemetry } from "./telemetry";
import type { AppConfig } from "./types";
import "./styles/app.css";

// Read the runtime config first (it names the OTLP path), but never wait long:
// the app must open even if the backend is down.
async function loadConfig(): Promise<AppConfig | null> {
  try {
    return await Promise.race([
      api.config(),
      new Promise<null>((resolve) => setTimeout(() => resolve(null), 2500)),
    ]);
  } catch {
    return null;
  }
}

loadConfig().then((config) => {
  if (config) startTelemetry(config);
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <BrowserRouter>
        <AuthProvider config={config}>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </StrictMode>,
  );
});
