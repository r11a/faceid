import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: resolve(import.meta.dirname, "../static"),
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: "assets/faceid-6.5.3.js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: asset => asset.name?.endsWith(".css") ? "assets/faceid-6.5.3[extname]" : "assets/[name]-[hash][extname]"
      }
    }
  }
});
