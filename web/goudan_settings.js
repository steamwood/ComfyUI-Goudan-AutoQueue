// 「缩放设置」「输出设置」：当前模式用不到的选项变灰（不能改，值照样保存）。
import { app } from "../../scripts/app.js";

const RULES = {
  GoudanScaleSettings: {
    control: "模式",
    disabledWhen: {
      "倍数": (value) => value !== "倍数",
      "长边像素": (value) => value !== "长边",
      "缩小时跳过SeedVR2（只缩放）": (value) => value !== "长边",
    },
  },
  GoudanOutputSettings: {
    control: "格式",
    disabledWhen: {
      "质量": (value) => value === "png",
    },
  },
};

function findWidget(node, name) {
  return (node.widgets || []).find((w) => w.name === name);
}

function applyRule(node, rule) {
  const control = findWidget(node, rule.control);
  if (!control) {
    return;
  }
  for (const [name, test] of Object.entries(rule.disabledWhen)) {
    const widget = findWidget(node, name);
    if (widget) {
      widget.disabled = test(control.value);
    }
  }
  if (node.setDirtyCanvas) {
    node.setDirtyCanvas(true, true);
  }
}

function hookNode(node, rule) {
  const control = findWidget(node, rule.control);
  if (!control || control.__goudanHooked) {
    applyRule(node, rule);
    return;
  }
  control.__goudanHooked = true;
  const original = control.callback;
  control.callback = function (...args) {
    const result = original ? original.apply(this, args) : undefined;
    applyRule(node, rule);
    return result;
  };
  applyRule(node, rule);
}

app.registerExtension({
  name: "Goudan.AutoQueue.Settings",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    const rule = RULES[nodeData.name];
    if (!rule) {
      return;
    }
    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function (...args) {
      const result = onNodeCreated ? onNodeCreated.apply(this, args) : undefined;
      hookNode(this, rule);
      return result;
    };
    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function (...args) {
      const result = onConfigure ? onConfigure.apply(this, args) : undefined;
      hookNode(this, rule);
      return result;
    };
  },
});
