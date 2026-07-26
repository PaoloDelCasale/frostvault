import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { ApiQueryProvider } from "@/api";
import { I18nProvider } from "@/i18n";
import { installDemoFilesFetch } from "@/pages/archive/demoFiles";
import App from "./App";
import "./index.css";

if (new URLSearchParams(window.location.search).get("demo") === "files") {
  installDemoFilesFetch();
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ApiQueryProvider>
      <I18nProvider>
        <App />
      </I18nProvider>
    </ApiQueryProvider>
  </StrictMode>,
);
