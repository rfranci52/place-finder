/// <reference types="vite/client" />

interface ImportMetaEnv {
  // Optional tile URL template; defaults to OpenStreetMap's standard tiles (see MapView.tsx).
  readonly VITE_TILE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
