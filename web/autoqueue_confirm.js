// 手动点“运行”前弹确认框：本次一共会排多少次（非空段数 × 轮数）。只拦界面上的点击，API 调用和自动排出的副本不受影响。
import { app } from "../../scripts/app.js";

function countSegments(text) {
  let n = 0, cur = false;
  for (const line of String(text || "").split("\n")) {
    if (line.startsWith("#")) { if (cur) n++; cur = false; }
    else if (line.trim()) cur = true;
  }
  if (cur) n++;
  return Math.max(1, n);
}

function wv(node, name) {
  const w = (node.widgets || []).find((x) => x.name === name);
  return w ? w.value : undefined;
}

function plannedRuns() {
  const g = app.graph;
  if (!g) return null;
  for (const node of g._nodes || []) {
    if (node.type !== "PromptCycleAutoQueue" || node.mode === 2 || node.mode === 4) continue;
    if (wv(node, "自动排队") !== true) continue;
    const rounds = Math.max(1, Number(wv(node, "轮数")) || 1);
    let segs = 1;
    for (const out of node.outputs || []) {
      for (const lid of out.links || []) {
        const link = g.links[lid] || (g.links.get && g.links.get(lid));
        const t = link && g.getNodeById(link.target_id);
        if (t && t.type === "ImpactStringSelector") segs = countSegments(wv(t, "strings"));
      }
    }
    return { segs, rounds, total: segs * rounds };
  }
  return null;
}

app.registerExtension({
  name: "Goudan.AutoQueue.Confirm",
  setup() {
    const orig = app.queuePrompt.bind(app);
    app.queuePrompt = async function (number, batchCount, ...rest) {
      try {
        const p = plannedRuns();
        if (p && p.total > 1) {
          const how = p.segs > 1 ? `${p.segs} 段 × ${p.rounds} 轮` : `单张抽卡 ${p.rounds} 轮`;
          const msg = `本次将运行 ${p.total} 次（${how}）。\n确定开始吗？`;
          if (!window.confirm(msg)) return false;
        }
      } catch (e) { console.warn("[狗蛋自动排队] 运行前提示出错，照常运行", e); }
      return orig(number, batchCount, ...rest);
    };
  },
});
