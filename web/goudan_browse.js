// 「SeedVR2 按路径读图」节点上的「选择文件夹…」「选择图片…」按钮：
// 让 ComfyUI 服务器（就是本机）弹出系统自带的选择窗口，把选中的路径写进「路径」框。取消就什么都不改。
// 「路径」框照样可以直接粘贴路径（带不带引号都行）。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_NAME = "SeedVR2DirLoad";
const PATH_WIDGET = "路径";
const ROUTE = "/goudan/browse";
const PASTE_HINT = "可以直接把路径粘贴到「路径」框里（带不带引号都行）。";
const BUTTONS = [
  { label: "选择文件夹…", kind: "dir" },
  { label: "选择图片…", kind: "file" },
];

function toast(severity, detail, life) {
  const manager = app.extensionManager && app.extensionManager.toast;
  if (manager && manager.add) {
    manager.add({ severity: severity, summary: "选择路径", detail: detail, life: life || 8000 });
  } else {
    console.log(`[选择路径] ${detail}`);
  }
}

function findWidget(node, name) {
  return (node.widgets || []).find((w) => w.name === name);
}

function setPath(node, widget, path) {
  widget.value = path;
  if (widget.callback) {
    widget.callback(path, app.canvas, node);
  }
  if (node.onWidgetChanged) {
    node.onWidgetChanged(widget.name, path, null, widget);
  }
  if (node.setDirtyCanvas) {
    node.setDirtyCanvas(true, true);
  }
  if (node.graph && node.graph.change) {
    node.graph.change();
  }
}

async function browse(node, kind) {
  const widget = findWidget(node, PATH_WIDGET);
  if (!widget) {
    return;
  }
  if (node.__goudanBrowsing) {
    toast("warn", "选择窗口已经开着，请先在那个窗口里选好或取消（它可能在别的窗口后面）。");
    return;
  }
  node.__goudanBrowsing = true;
  let response = null;
  let data = {};
  try {
    response = await api.fetchApi(ROUTE, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: kind, current: String(widget.value || "") }),
    });
    try {
      data = await response.json();
    } catch (e) {
      data = {};
    }
  } catch (e) {
    toast("error", `连不上 ComfyUI 服务器：${e}。${PASTE_HINT}`);
    return;
  } finally {
    node.__goudanBrowsing = false;
  }
  if (data.status === "ok" && data.path) {
    setPath(node, widget, data.path);
    return;
  }
  if (data.status === "cancelled") {
    return;
  }
  if (response.status === 409 || data.status === "busy") {
    toast("warn", data.error || "已经有一个选择窗口开着，请先在那个窗口里选好或取消。");
    return;
  }
  if (response.status === 404) {
    toast("error", `服务器上没有选择窗口功能（请重启 ComfyUI 让新版节点生效）。${PASTE_HINT}`, 12000);
    return;
  }
  const detail = data.detail ? `（${data.detail}）` : "";
  toast("error", `${data.error || "打不开选择窗口（HTTP " + response.status + "）。" + PASTE_HINT}${detail}`, 12000);
}

function addButtons(node) {
  if (node.__goudanBrowseButtons) {
    return;
  }
  node.__goudanBrowseButtons = true;
  for (const def of BUTTONS) {
    // 按钮放在最后、不保存进工作流（serialize=false），旧工作流的控件值顺序不受影响
    const button = node.addWidget("button", def.label, null, () => {
      browse(node, def.kind);
    }, { serialize: false });
    button.serialize = false;
  }
  fitSize(node);
}

function fitSize(node) {
  // 旧工作流里保存的节点高度没算按钮，打开时自动加高，按钮不会露到节点外面
  const size = node.computeSize ? node.computeSize() : null;
  if (size && node.size && node.setSize) {
    if (size[0] > node.size[0] || size[1] > node.size[1]) {
      node.setSize([Math.max(node.size[0], size[0]), Math.max(node.size[1], size[1])]);
    }
  }
}

app.registerExtension({
  name: "Goudan.AutoQueue.Browse",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE_NAME) {
      return;
    }
    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function (...args) {
      const result = onNodeCreated ? onNodeCreated.apply(this, args) : undefined;
      addButtons(this);
      return result;
    };
    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function (...args) {
      const result = onConfigure ? onConfigure.apply(this, args) : undefined;
      addButtons(this);
      fitSize(this);
      return result;
    };
  },
});

export { browse, PATH_WIDGET, ROUTE };
