import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "@fontsource/heebo/400.css";
import "@fontsource/heebo/500.css";
import "@fontsource/heebo/600.css";
import "./styles.css";
import "./additions.css";
import "./home-workspace.css";
import "./daily-workspace.css";
import "./workspace.css";

createRoot(document.getElementById("root")).render(
  <React.StrictMode><App /></React.StrictMode>,
);
