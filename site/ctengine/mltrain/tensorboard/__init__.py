# ensure users can still use a non-torch mltrain version
try:
    from ctengine.mltrain.tensorboard.lightgbm_callback import LightGBMTensorboardCallback
    from ctengine.mltrain.tensorboard.tensorboard import TensorBoardCallback, TensorboardLogger

    TBLogger = TensorboardLogger
    TBCallback = TensorBoardCallback
    LightGBMCallback = LightGBMTensorboardCallback
except ModuleNotFoundError:
    from ctengine.mltrain.tensorboard.base_tensorboard import (
        BaseTensorBoardCallback,
        BaseTensorboardLogger,
    )

    TBLogger = BaseTensorboardLogger  # type: ignore
    TBCallback = BaseTensorBoardCallback  # type: ignore
    LightGBMCallback = None  # type: ignore

__all__ = ("TBLogger", "TBCallback", "LightGBMCallback")
