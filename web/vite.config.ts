import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发 / E2E 期后端地址（确定性 dev backend，见 e2e/）。
// 生产环境前端置于 Nginx 之后，Nginx 注入 X-User-Id 头并反代 /api 与 /ws。
const BACKEND_HTTP =
  process.env.VITE_BACKEND_URL ?? "http://127.0.0.1:8001";
const BACKEND_WS = process.env.VITE_WS_BACKEND_URL ?? "ws://127.0.0.1:8001";
const DEV_USER = process.env.VITE_DEV_USER ?? "dev-user";

// 开发代理：把 /api（HTTP）与 /ws（WebSocket）反代到后端，并注入一期信任的
// X-User-Id 头（PRD §3.2：一期信任 Nginx 注入；本地无 Nginx，故由代理注入）。
function injectUser(proxy: { on: (ev: string, fn: (req: { setHeader: (k: string, v: string) => void }) => void) => void }) {
  const add = (req: { setHeader: (k: string, v: string) => void }) => {
    req.setHeader("X-User-Id", DEV_USER);
  };
  proxy.on("proxyReq", add);
  proxy.on("proxyReqWs", add);
}

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: BACKEND_HTTP,
        changeOrigin: true,
        configure: injectUser,
      },
      "/ws": {
        target: BACKEND_WS,
        ws: true,
        changeOrigin: true,
        configure: injectUser,
      },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
