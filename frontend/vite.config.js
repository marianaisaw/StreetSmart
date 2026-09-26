import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dev: `npm run dev` proxies /api to the FastAPI server on :8765. Prod: `npm run build`, FastAPI serves dist/.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': 'http://localhost:8765' } },
});
