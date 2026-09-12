/// <reference types="astro/client" />

interface ImportMetaEnv {
  readonly PUBLIC_API_BASE?: string;
  readonly PUBLIC_MEDIA_BASE?: string;
  readonly USE_FIXTURE?: string;
  readonly API_TIMEOUT_MS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
