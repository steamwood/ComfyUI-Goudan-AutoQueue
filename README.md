# ComfyUI-Goudan-AutoQueue

点一次运行就自动排完整批任务的两组 ComfyUI 节点：

- **提示词循环（自动计条数 + 自动排队）** `PromptCycleAutoQueue`
  连到 Impact Pack 的 String Selector（多行模式），自动数出提示词条数（以单独一行 `#` 分隔），只填轮数，点一次运行就把 条数 × 轮数 次全部排进队列，按顺序轮换，每次重新随机种子。
- **SeedVR2 按路径读图（逐张，自动排队）** `SeedVR2DirLoad` + **SeedVR2 保存到原目录/SeedVR2** `SeedVR2DirSave`
  粘贴任意文件夹或单张图片路径，逐张放大，结果存到 `<原目录>/SeedVR2/`，保留原文件名并加后缀；已放大的自动跳过，可断点续跑。

## 安装
ComfyUI Manager → Install via Git URL，填本仓库地址；或手动：

```
cd ComfyUI/custom_nodes
git clone <本仓库地址>
```

重启 ComfyUI。无额外依赖。示例工作流在 `example_workflows/`。

## 注意
- 一批没跑完时不要再点运行，否则会再排一整批；想停下就清空队列。
- 节点上的「序号」保持 0。
