import { defineConfig, devices } from "@playwright/test";

const REPO = "/home/karl/repos/work/HypoArgus";

/**
 * E2E 配置（T-07）——两个 webServer：
 *   1. Python 确定性 dev 后端（端口 8001，StreamingFakeChat + InterruptHitl*Gate + PG）；
 *   2. Vite dev 前端（端口 5173），其 /api 与 /ws 代理注入 X-User-Id 到 8001。
 * Playwright 命中 5173；前端用相对 URL，故开发 / E2E / 生产（Nginx 注入头）同一路径。
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: "http://127.0.0.1:5173",
    ...devices["Desktop Chrome"],
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command: "conda run --no-capture-output -n HypoArgus python e2e/dev_server.py",
      cwd: REPO,
      url: "http://127.0.0.1:8001/api/agent/graph",
      timeout: 90_000,
      reuseExistingServer: true,
    },
    {
      command: "./node_modules/.bin/vite --port 5173 --strictPort",
      cwd: `${REPO}/web`,
      url: "http://127.0.0.1:5173",
      timeout: 60_000,
      reuseExistingServer: true,
    },
  ],
});
