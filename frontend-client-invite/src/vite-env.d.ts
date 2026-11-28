/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_USE_MOCK_API?: string;
  readonly VITE_CLIENT_INVITE_API_URL?: string;
  readonly VITE_CLIENT_INVITE_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
