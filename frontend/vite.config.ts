import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";

// Builds the CSS+JS bundle into Django's static dir. The entry keeps fixed filenames; cache-busting is
// handled by Django/WhiteNoise's ManifestStaticFilesStorage at collectstatic time in production.
//
// Code loaded on demand (import(), e.g. the Hjemmeside editor) becomes chunks/[name]-[hash].js. Their
// names carry their own content hash, so they can be cached for good, and collectstatic keeps the
// unhashed copy app.js's import() asks for next to its hashed one (ManifestStaticFilesStorage stores
// both, and does not rewrite paths inside JavaScript).
//
// modulePreload off: Vite would otherwise preload a chunk's dependencies from absolute /assets/…
// URLs, which do not exist under /static/dist/. The chunks are fetched when they are imported.
export default defineConfig({
  plugins: [tailwindcss()],
  build: {
    outDir: "../app/static/dist",
    emptyOutDir: true,
    modulePreload: false,
    rollupOptions: {
      input: "src/main.ts",
      output: {
        entryFileNames: "app.js",
        chunkFileNames: "chunks/[name]-[hash].js",
        assetFileNames: "app.[ext]",
      },
    },
  },
});
