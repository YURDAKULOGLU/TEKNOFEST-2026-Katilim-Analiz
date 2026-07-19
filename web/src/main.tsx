import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { createApiClient } from "./api/client";
import { App } from "./app/App";
import { createAppQueryClient } from "./app/query-client";
import "./styles/global.css";

const rootElement = document.getElementById("root");

if (!rootElement) {
  throw new Error("Uygulama kök öğesi bulunamadı.");
}

const client = createApiClient();
const queryClient = createAppQueryClient();

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App client={client} />
    </QueryClientProvider>
  </StrictMode>,
);
