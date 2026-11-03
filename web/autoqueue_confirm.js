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

// 按导出的 API 图（app.graphToPrompt 的 output）来算：子图会被展开成 "405:18" 这样的 id，
// 绕过（bypass）的节点也已经被去掉，所以子图打包与否都一样。
function isOutputClass(ct) {
  const LG = window.LiteGraph || (window.comfyAPI && window.comfyAPI.litegraph && window.comfyAPI.litegraph.LiteGraph);
  const T = LG && LG.registered_node_types && LG.registered_node_types[ct];
  return !!(T && T.nodeData && T.nodeData.output_node);
}

function isLink(v) {
  return Array.isArray(v) && v.length === 2 && typeof v[0] === "string";
}

// 开关当前的值：直接写的布尔值，或者连进来的布尔节点（只有一个常量输入）的值；读不出返回 undefined（两路都算）
function switchValue(p, node) {
  let v = node.inputs ? node.inputs.switch : undefined;
  for (let i = 0; i < 4 && isLink(v); i++) {
    const src = p[v[0]];
    const vals = src ? Object.values(src.inputs || {}).filter((x) => typeof x !== "object" || x === null) : [];
    v = vals.length === 1 ? vals[0] : undefined;
  }
  return typeof v === "boolean" ? v : undefined;
}

function downstream(p) {
  const m = {};
  for (const [id, n] of Object.entries(p)) {
    for (const [name, v] of Object.entries(n.inputs || {})) {
      if (isLink(v)) (m[v[0]] = m[v[0]] || []).push([id, name]);
    }
  }
  return m;
}

// 从 start 往下游走，开关节点只走被选中的那一路；走到输出节点（保存/预览）就算“在用”
function reachesOutput(p, down, start) {
  const seen = new Set([start]);
  const todo = [start];
  let blocked = false;
  while (todo.length) {
    const id = todo.pop();
    for (const [t, name] of down[id] || []) {
      if (seen.has(t)) continue;
      const tn = p[t];
      if (tn.class_type === "ComfySwitchNode") {
        const v = switchValue(p, tn);
        if (v !== undefined && ((name === "on_true" && !v) || (name === "on_false" && v))) { blocked = true; continue; }
      }
      if (isOutputClass(tn.class_type)) return true;
      seen.add(t);
      todo.push(t);
    }
  }
  // 没碰到被关掉的分支（比如图里没有保存节点）就按老规矩算“在用”
  return !blocked;
}

async function plannedRuns() {
  if (!app.graph) return null;
  const p = (await app.graphToPrompt()).output || {};
  const down = downstream(p);
  for (const [id, n] of Object.entries(p)) {
    if (n.class_type !== "PromptCycleAutoQueue") continue;
    const inp = n.inputs || {};
    if (inp["自动排队"] !== true) continue;
    if (!reachesOutput(p, down, id)) continue;
    const rounds = Math.max(1, Number(inp["轮数"]) || 1);
    let segs = 1;
    for (const [t] of down[id] || []) {
      if (p[t].class_type === "ImpactStringSelector") segs = countSegments(p[t].inputs.strings);
    }
    return { kind: "prompt", segs, rounds, total: segs * rounds };
  }
  for (const [id, n] of Object.entries(p)) {
    if (n.class_type !== "SeedVR2DirLoad") continue;
    if (!reachesOutput(p, down, id)) continue;
    const inp = n.inputs || {};
    const path = String(inp["路径"] || "").trim().replace(/^["']|["']$/g, "");
    if (!path || IMAGE_EXT.test(path)) return { kind: "upscale", folder: false, path, total: path ? 1 : 0 };
    return { kind: "upscale", folder: true, path, auto: inp["自动排队"] === true };
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
    const orig = app.queuePrompt;
    app.queuePrompt = async function (number, batchCount, ...rest) {
      try {
        const p = await plannedRuns();
        if (p && (p.folder || (p.kind === "prompt" && p.total > 1))) {
          if (!window.confirm(confirmText(p))) return false;
        }
      } catch (e) { console.warn("[狗蛋自动排队] 运行前提示出错，照常运行", e); }
      return orig.call(app, number, batchCount, ...rest);
    };
  },
});
