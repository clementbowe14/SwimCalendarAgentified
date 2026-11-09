/// <reference types="vite/client" />

declare module "*.css";

interface ViteTypeOptions {
  strictImportMetaEnv: unknown;
}

interface ImportMetaEnv {
  readonly VITE_USE_MOCK_API?: string;
  readonly VITE_INSTRUCTOR_API_URL?: string;
  readonly VITE_INSTRUCTOR_ID?: string;
  readonly VITE_INSTRUCTOR_COMMAND_DATE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
