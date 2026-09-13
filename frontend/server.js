/**
 * Minimal static file server for the frontend.
 *
 * Why not just serve index.html as a raw static file with a hardcoded
 * API URL? Because that URL is only known after the backend service is
 * deployed on Railway, and it can change (redeploys, custom domains).
 * Instead, the browser loads /config.js first, which this server
 * generates on every request from the API_BASE_URL environment
 * variable — so changing that env var in Railway and restarting the
 * service is enough to repoint the frontend, no rebuild required.
 */
const express = require("express");
const path = require("path");

const app = express();
const PORT = process.env.PORT || 3000;
const API_BASE_URL = process.env.API_BASE_URL || "http://localhost:8000";

app.get("/config.js", (req, res) => {
  res.type("application/javascript");
  res.send(`window.__API_BASE__ = ${JSON.stringify(API_BASE_URL)};`);
});

app.use(express.static(path.join(__dirname, "public")));

app.listen(PORT, "0.0.0.0", () => {
  console.log(`Frontend listening on port ${PORT}`);
  console.log(`API_BASE_URL = ${API_BASE_URL}`);
});
