# ComfyUI-Goudan-AutoQueue

点一次运行就自动排完整批任务的几组 ComfyUI 节点：

- **提示词循环（自动计条数 + 自动排队）** `PromptCycleAutoQueue`
  连到 Impact Pack 的 String Selector（多行模式），自动数出提示词条数（以单独一行 `#` 分隔），只填轮数，点一次运行就把 条数 × 轮数 次全部排进队列，按顺序轮换，每次重新随机种子。
- **文件名/文件夹（自动）** `PromptCycleFilenames`
  「序号」「条数」连到提示词循环，一个节点直接输出三个保存路径前缀，分别连三个「保存图像」的文件名前缀：`日期/01_Krea2-2ST`、`日期/01_Krea2-USD`、`日期/seedvr/01_seedvr2_after`。编号默认两位（01、02…），超过 99 条自动变成三位。后缀和放大子文件夹名都可以在节点上改。
  「日期」默认 `%date:yyyy-MM-dd%`，在点运行那一刻确定，整批自动排队的副本都用同一个日期，跑过零点也不会分到两个文件夹；也可以直接写固定文字当文件夹名。
- **SeedVR2 按路径读图（逐张，自动排队）** `SeedVR2DirLoad` + **SeedVR2 保存到原目录/SeedVR2** `SeedVR2DirSave`
  粘贴任意文件夹或单张图片路径，逐张放大，结果存到 `<原目录>/SeedVR2/`，保留原文件名并加后缀；已放大的自动跳过，可断点续跑。

## 安装
ComfyUI Manager → Install via Git URL，填 `https://github.com/steamwood/ComfyUI-Goudan-AutoQueue.git`；或手动：

```
cd ComfyUI/custom_nodes
git clone https://github.com/steamwood/ComfyUI-Goudan-AutoQueue.git
```

重启 ComfyUI。无额外依赖。示例工作流在 `example_workflows/`。

## 注意
- 一批没跑完时可以改提示词/模型/LoRA/轮数/文件夹再点运行，新的一整批会排在上一批后面，按顺序跑完。
- 内容完全没改又点了一次（误连点）会自动整次跳过，不会重复出图；想停下就清空队列。
- 节点上的「序号」保持 0。
- 「文件名/文件夹（自动）」的日期用的是运行 ComfyUI 那台电脑的时间（格式同保存图像节点的 `%date:...%`：yyyy 年、MM 月、dd 日、hh 时、mm 分、ss 秒）。
