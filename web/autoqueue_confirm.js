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

function idKey(id) {
  return String(id).split(":").map((x) => (/^\d+$/.test(x) ? Number(x).toString().padStart(12, "0") : x)).join(":");
}

// 和服务端一样：多个开着「自动排队」的提示词循环节点时只由 id 最小的那个排队
function activeNodes(p, down, type) {
  return Object.entries(p)
    .filter(([id, n]) => n.class_type === type && (n.inputs || {})["自动排队"] === true && reachesOutput(p, down, id))
    .sort((a, b) => (idKey(a[0]) < idKey(b[0]) ? -1 : 1));
}

function relayLines(text) {
  return String(text || "").split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("#")).length;
}

async function plannedRuns() {
  if (!app.graph) return null;
  const p = (await app.graphToPrompt()).output || {};
  const down = downstream(p);
  const relay = activeNodes(p, down, "GoudanSegmentRelay")[0];
  if (relay) {
    const inp = relay[1].inputs || {};
    const rounds = Math.max(1, Number(inp["轮数"]) || 1);
    const want = Number(inp["段数"]) || 0;
    const segs = Math.max(1, want > 0 ? want : relayLines(inp["分段提示词"]));
    return { kind: "relay", segs, rounds, total: segs * rounds };
  }
  const cyc = activeNodes(p, down, "PromptCycleAutoQueue");
  if (cyc.length) {
    const [id, n] = cyc[0];
    const inp = n.inputs || {};
    const rounds = Math.max(1, Number(inp["轮数"]) || 1);
    let segs = 1;
    for (const [t] of down[id] || []) {
      const sn = p[t];
      if (sn.class_type === "ImpactStringSelector" && String((sn.inputs || {}).select && sn.inputs.select[0]) === String(id)) {
        segs = sn.inputs.multiline === false ? Math.max(1, String(sn.inputs.strings || "").split("\n").length) : countSegments(sn.inputs.strings);
      }
    }
    return { kind: "prompt", segs, rounds, total: segs * rounds, extra: cyc.length - 1 };
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

function confirmText(p, batch) {
  const times = batch > 1 ? `\n（批量次数 ${batch}：上面的整套会跑 ${batch} 遍，共 ${p.total * batch} 次）` : "";
  if (p.kind === "relay") {
    return `分段接力：本次将运行 ${p.total} 个任务（${p.rounds} 轮 × ${p.segs} 段，按 轮→段 顺序）。${times}\n确定开始吗？`;
  }
  if (p.kind === "upscale") {
    const how = p.auto ? "逐张自动排队，已放大过的会跳过" : "自动排队关着，只跑这一次";
    return `只放大模式：处理文件夹\n${p.path}\n里的图片（${how}；不按提示词段数 × 轮数算）。\n确定开始吗？`;
  }
  const how = p.segs > 1 ? `${p.segs} 段 × ${p.rounds} 轮` : `单张抽卡 ${p.rounds} 轮`;
  const more = p.extra ? `\n注意：图里还有 ${p.extra} 个开着自动排队的提示词循环节点，只有 id 最小的那个会排队。` : "";
  return `本次将运行 ${p.total} 次（${how}）。${times}${more}\n确定开始吗？`;
}

app.registerExtension({
  name: "Goudan.AutoQueue.Confirm",
  setup() {
    const orig = app.queuePrompt;
    app.queuePrompt = async function (number, batchCount, ...rest) {
      try {
        const p = await plannedRuns();
        const batch = Math.max(1, Number(batchCount) || 1);
        if (p && (p.folder || ((p.kind === "prompt" || p.kind === "relay") && p.total * batch > 1 && p.total > 1))) {
          if (!window.confirm(confirmText(p, batch))) return false;
        }
      } catch (e) { console.warn("[狗蛋自动排队] 运行前提示出错，照常运行", e); }
      return orig.call(app, number, batchCount, ...rest);
    };
  },
});
