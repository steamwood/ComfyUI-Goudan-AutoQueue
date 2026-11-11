"""狗蛋工具：SeedVR2 按路径逐张放大 + 提示词循环自动排队 + 自动文件名。"""
from . import browse  # noqa: F401  注册 /goudan/browse（选择文件夹/图片按钮）
from .dirio import SeedVR2DirLoad, SeedVR2DirSave
from .autoqueue import PromptCycleAutoQueue
from .segrelay import GoudanSegmentRelay
from .h3anchor import GoudanH3SceneAnchor
from .filenames import PromptCycleFilenames
from .scaling import GoudanScaleSettings, GoudanScalePrepare, GoudanScaleFinish
from .saving import GoudanOutputSettings, GoudanSaveImage
from .videoup import GoudanVideoDirLoad, GoudanVideoDirSave, GoudanFrameModelUpscale, GoudanVideoLongSide

NODE_CLASS_MAPPINGS = {
    "SeedVR2DirLoad": SeedVR2DirLoad,
    "SeedVR2DirSave": SeedVR2DirSave,
    "PromptCycleAutoQueue": PromptCycleAutoQueue,
    "PromptCycleFilenames": PromptCycleFilenames,
    "GoudanSegmentRelay": GoudanSegmentRelay,
    "GoudanH3SceneAnchor": GoudanH3SceneAnchor,
    "GoudanScaleSettings": GoudanScaleSettings,
    "GoudanScalePrepare": GoudanScalePrepare,
    "GoudanScaleFinish": GoudanScaleFinish,
    "GoudanOutputSettings": GoudanOutputSettings,
    "GoudanSaveImage": GoudanSaveImage,
    "GoudanVideoDirLoad": GoudanVideoDirLoad,
    "GoudanVideoDirSave": GoudanVideoDirSave,
    "GoudanFrameModelUpscale": GoudanFrameModelUpscale,
    "GoudanVideoLongSide": GoudanVideoLongSide,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "SeedVR2DirLoad": "SeedVR2 按路径读图（逐张，自动排队）",
    "SeedVR2DirSave": "SeedVR2 保存到原目录/SeedVR2",
    "PromptCycleAutoQueue": "提示词循环（自动计条数 + 自动排队）",
    "PromptCycleFilenames": "文件名/文件夹（自动）",
    "GoudanSegmentRelay": "分段批量排队（每段一个任务）",
    "GoudanH3SceneAnchor": "场景锚定（颜色/对比/细节拉回场景第 1 段）",
    "GoudanScaleSettings": "缩放设置",
    "GoudanScalePrepare": "SeedVR2 前缩放（按缩放设置）",
    "GoudanScaleFinish": "最终尺寸（精确缩放 / 缩小可跳过SeedVR2）",
    "GoudanOutputSettings": "输出设置",
    "GoudanSaveImage": "保存图像（按输出设置）",
    "GoudanVideoDirLoad": "批量视频放大·按路径读视频（逐段，自动排队）",
    "GoudanVideoDirSave": "批量视频放大·保存到原目录/SeedVR2（分段续跑+保留音轨帧率）",
    "GoudanFrameModelUpscale": "逐帧模型放大→缩到目标长边（省内存）",
    "GoudanVideoLongSide": "视频帧缩放到长边",
}
WEB_DIRECTORY = "./web"
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
