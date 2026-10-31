"""狗蛋工具：SeedVR2 按路径逐张放大 + 提示词循环自动排队 + 自动文件名。"""
from .dirio import SeedVR2DirLoad, SeedVR2DirSave
from .autoqueue import PromptCycleAutoQueue
from .filenames import PromptCycleFilenames

NODE_CLASS_MAPPINGS = {
    "SeedVR2DirLoad": SeedVR2DirLoad,
    "SeedVR2DirSave": SeedVR2DirSave,
    "PromptCycleAutoQueue": PromptCycleAutoQueue,
    "PromptCycleFilenames": PromptCycleFilenames,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "SeedVR2DirLoad": "SeedVR2 按路径读图（逐张，自动排队）",
    "SeedVR2DirSave": "SeedVR2 保存到原目录/SeedVR2",
    "PromptCycleAutoQueue": "提示词循环（自动计条数 + 自动排队）",
    "PromptCycleFilenames": "文件名/文件夹（自动）",
}
