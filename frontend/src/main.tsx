import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ErrorBoundary, PageRecovery } from "./ErrorBoundary";
import "katex/dist/katex.min.css";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary fallback={<PageRecovery />}><App /></ErrorBoundary>
  </StrictMode>,
);
