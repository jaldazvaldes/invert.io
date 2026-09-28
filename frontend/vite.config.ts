import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// En desarrollo (`npm run dev`) las llamadas a la API van al motor en el puerto 8000.
// En producción el propio motor sirve estos ficheros (frontend/dist).
const backend = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: backend, ws: true, changeOrigin: true },
      "/reports": { target: backend, changeOrigin: true },
      "/lab": { target: backend, changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
