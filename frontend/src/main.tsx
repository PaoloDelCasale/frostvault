import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { ApiQueryProvider } from "@/api";
import { I18nProvider } from "@/i18n";
import { ThemeProvider } from "@/theme";
import { ReauthPasswordGate } from "@/pages/archive/ReauthPasswordGate";
import { retireOfflineCache } from "./retireOfflineCache";
import App from "./App";
import "./index.css";

// Finish one-way legacy cleanup before mounting. In particular a visible
// online UI must not race a still-running unregister against a browser upgrade.
void retireOfflineCache().finally(() => createRoot(document.getElementById("root")!).render(
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
));
