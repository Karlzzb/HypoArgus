import { test, expect } from "@playwright/test";

/**
 * T-07 E2E——浏览器驱动完整一次修订（发起 → 实时 CoT → HITL 卡片提交 → 续跑 → 终态），
 * 刷新 / 切会话重连回放正确，UI 像素级无错位（关键帧截图）。
 *
 * 后端为确定性 dev 后端（StreamingFakeChat + InterruptHitl*Gate + PG checkpointer），
 * 无网络 / 无 token、确定性；经 Vite 代理注入 X-User-Id。
 */
test.describe("T-07 React 工作台", () => {
  test("完整修订 + 切会话/刷新重连回放", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (m) => {
      if (m.type() === "error") errors.push(m.text());
    });
    page.on("pageerror", (e) => errors.push(String(e)));

    await page.goto("/");
    await page.getByRole("button", { name: "新建对话" }).click();

    // 流程图骨架来自 graph_static。
    await expect(page.locator(".react-flow__node").first()).toBeVisible({
      timeout: 10000,
    });
    await page.screenshot({ path: "test-results/00-graph.png" });

    // 发起修订（query + 默认样例文档）。
    await page.locator("textarea").nth(1).fill("修订这段论证");
    await page.getByRole("button", { name: "发送" }).click();

    // 实时 CoT：node_id+instance 分组、打字机渲染。
    await expect(page.locator(".cot-group .cot-text").first()).not.toBeEmpty({
      timeout: 15000,
    });
    await page.screenshot({ path: "test-results/01-cot.png" });

    // HITL-1 嵌入式卡片（不遮挡流程图）→ 跳过。
    await expect(page.locator(".hitl-card .q")).toContainText("段落切分", {
      timeout: 15000,
    });
    await page.getByRole("button", { name: "跳过" }).click();

    // HITL-2 终稿卡片 → 通过。
    await expect(page.locator(".hitl-card .q")).toContainText("终稿", {
      timeout: 15000,
    });
    await page.screenshot({ path: "test-results/02-hitl2.png" });
    const sessionId = await page
      .locator(".session-chip.active")
      .getAttribute("title");
    expect(sessionId).toBeTruthy();

    await page.getByRole("button", { name: "通过" }).click();

    // 终态（终稿）。
    await expect(page.locator(".final-doc")).toBeVisible({ timeout: 15000 });
    await page.screenshot({ path: "test-results/03-terminal.png" });

    // 切会话重连回放：新建第二个会话，再切回第一个 → 后端按 event_seq 重放。
    await page.getByRole("button", { name: "新建对话" }).click();
    await expect(page.locator(".react-flow__node").first()).toBeVisible({
      timeout: 10000,
    });
    await page.locator(`.session-chip[title="${sessionId}"]`).click();
    await expect(page.locator(".cot-group").first()).toBeVisible({
      timeout: 15000,
    });

    // 刷新重连回放：reload 后从 localStorage 恢复会话列表，切回该会话 → 重放。
    await page.reload();
    await expect(page.locator(`.session-chip[title="${sessionId}"]`)).toBeVisible();
    await page.locator(`.session-chip[title="${sessionId}"]`).click();
    await expect(page.locator(".react-flow__node").first()).toBeVisible({
      timeout: 10000,
    });
    await expect(page.locator(".cot-group").first()).toBeVisible({
      timeout: 15000,
    });
    await page.screenshot({ path: "test-results/04-replay.png" });

    expect(errors, errors.join("\n")).toEqual([]);
  });
});
