import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { ApiQueryProvider } from "@/api";
import { DEMO_MODE_ENABLED, getDemoSearchParam } from "@/demoGate";
import { I18nProvider } from "@/i18n";
import { ThemeProvider } from "@/theme";
import { installDemoFilesFetch } from "@/pages/archive/demoFiles";
import { ReauthPasswordGate } from "@/pages/archive/ReauthPasswordGate";
import { registerFrostVaultServiceWorker } from "@/pwa";
import App from "./App";
import "./index.css";

registerFrostVaultServiceWorker();

if (DEMO_MODE_ENABLED && getDemoSearchParam("demo") === "files") {
  installDemoFilesFetch();
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ApiQueryProvider>
      <I18nProvider>
        <ThemeProvider>
          <ReauthPasswordGate>
            <App />
          </ReauthPasswordGate>
        </ThemeProvider>
      </I18nProvider>
    </ApiQueryProvider>
  </StrictMode>,
);
