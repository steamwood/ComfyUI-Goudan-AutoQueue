// 手动点“运行”前弹确认框：本次一共会排多少次。只拦界面上的点击，API 调用和自动排出的副本不受影响。
// 按“实际会跑的那条流程”算：提示词循环节点的输出只连到没被选中的开关分支（例如“只放大”模式）时，不按段数 × 轮数算，
// 改为看正在用的「SeedVR2 读图」节点：单张图片不弹框，文件夹提示逐张处理。
import { app } from "../../scripts/app.js";

const IMAGE_EXT = /\.(png|jpe?g|webp|bmp|tiff?|gif)$/i;

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

function getLink(g, lid) {
  return g.links[lid] || (g.links.get && g.links.get(lid));
}

function bypassed(node) {
  return node.mode === 2 || node.mode === 4;
}

function isOutputNode(node) {
  const d = node.constructor && node.constructor.nodeData;
  return !!(d && d.output_node);
}

// 开关节点当前的值：控件值，或者连进来的布尔节点的值；读不出返回 undefined（两路都算）
function switchValue(g, node) {
  const inp = (node.inputs || []).find((i) => i.name === "switch");
  if (inp && inp.link != null) {
    const l = getLink(g, inp.link);
    const src = l && g.getNodeById(l.origin_id);
    const v = src && src.widgets && src.widgets[0] ? src.widgets[0].value : undefined;
    return typeof v === "boolean" ? v : undefined;
  }
  const v = wv(node, "switch");
  return typeof v === "boolean" ? v : undefined;
}

// 从 start 往下游走，开关节点只走被选中的那一路；走到输出节点（保存/预览）就算“在用”
function reachesOutput(g, start) {
  const seen = new Set([start.id]);
  const todo = [start];
  let blocked = false;
  while (todo.length) {
    const node = todo.pop();
    for (const out of node.outputs || []) {
      for (const lid of out.links || []) {
        const l = getLink(g, lid);
        const t = l && g.getNodeById(l.target_id);
        if (!t || bypassed(t) || seen.has(t.id)) continue;
        if (t.type === "ComfySwitchNode") {
          const name = t.inputs && t.inputs[l.target_slot] ? t.inputs[l.target_slot].name : "";
          const v = switchValue(g, t);
          if (v !== undefined && ((name === "on_true" && !v) || (name === "on_false" && v))) { blocked = true; continue; }
        }
        if (isOutputNode(t)) return true;
        seen.add(t.id);
        todo.push(t);
      }
    }
  }
  // 没碰到被关掉的分支（比如图里没有保存节点）就按老规矩算“在用”
  return !blocked;
}

function plannedRuns() {
  const g = app.graph;
  if (!g) return null;
  for (const node of g._nodes || []) {
    if (node.type !== "PromptCycleAutoQueue" || bypassed(node)) continue;
    if (wv(node, "自动排队") !== true) continue;
    if (!reachesOutput(g, node)) continue;
    const rounds = Math.max(1, Number(wv(node, "轮数")) || 1);
    let segs = 1;
    for (const out of node.outputs || []) {
      for (const lid of out.links || []) {
        const link = getLink(g, lid);
        const t = link && g.getNodeById(link.target_id);
        if (t && t.type === "ImpactStringSelector") segs = countSegments(wv(t, "strings"));
      }
    }
    return { kind: "prompt", segs, rounds, total: segs * rounds };
  }
  for (const node of g._nodes || []) {
    if (node.type !== "SeedVR2DirLoad" || bypassed(node)) continue;
    if (!reachesOutput(g, node)) continue;
    const path = String(wv(node, "路径") || "").trim().replace(/^["']|["']$/g, "");
    if (!path || IMAGE_EXT.test(path)) return { kind: "upscale", folder: false, path, total: path ? 1 : 0 };
    return { kind: "upscale", folder: true, path, auto: wv(node, "自动排队") === true };
  }
  return null;
}

function confirmText(p) {
  if (p.kind === "upscale") {
    const how = p.auto ? "逐张自动排队，已放大过的会跳过" : "自动排队关着，只跑这一次";
    return `只放大模式：处理文件夹\n${p.path}\n里的图片（${how}；不按提示词段数 × 轮数算）。\n确定开始吗？`;
  }
  const how = p.segs > 1 ? `${p.segs} 段 × ${p.rounds} 轮` : `单张抽卡 ${p.rounds} 轮`;
  return `本次将运行 ${p.total} 次（${how}）。\n确定开始吗？`;
}

app.registerExtension({
  name: "Goudan.AutoQueue.Confirm",
  setup() {
    const orig = app.queuePrompt.bind(app);
    app.queuePrompt = async function (number, batchCount, ...rest) {
      try {
        const p = plannedRuns();
        if (p && (p.folder || (p.kind === "prompt" && p.total > 1))) {
          if (!window.confirm(confirmText(p))) return false;
        }
      } catch (e) { console.warn("[狗蛋自动排队] 运行前提示出错，照常运行", e); }
      return orig(number, batchCount, ...rest);
    };
  },
});
