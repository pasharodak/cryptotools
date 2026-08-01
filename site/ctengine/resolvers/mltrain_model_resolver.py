# pragma pylint: disable=attribute-defined-outside-init

"""
This module load a custom model for mltrain
"""

import logging
from pathlib import Path

from ctengine.constants import USERPATH_MLTRAIN_MODELS, Config
from ctengine.exceptions import OperationalException
from ctengine.mltrain.mltrain_interface import IMltrainModel
from ctengine.resolvers import IResolver


logger = logging.getLogger(__name__)


class MltrainModelResolver(IResolver):
    """
    This class contains all the logic to load custom hyperopt loss class
    """

    object_type = IMltrainModel
    object_type_str = "MltrainModel"
    user_subdir = USERPATH_MLTRAIN_MODELS
    initial_search_path = (
        Path(__file__).parent.parent.joinpath("mltrain/prediction_models").resolve()
    )
    extra_path = "mltrain_model_path"

    @staticmethod
    def load_mltrain_model(config: Config) -> IMltrainModel:
        """
        Load the custom class from config parameter
        :param config: configuration dictionary
        """
        disallowed_models = ["BaseRegressionModel"]

        mltrain_model_name = config.get("mltrain_model")
        if not mltrain_model_name:
            raise OperationalException(
                "No mltrain_model set. Please use `--mltrain_model` to "
                "specify the MltrainModel class to use.\n"
            )
        if mltrain_model_name in disallowed_models:
            raise OperationalException(
                f"{mltrain_model_name} is a baseclass and cannot be used directly. Please choose "
                "an existing child class or inherit from this baseclass.\n"
            )
        mltrain_model = MltrainModelResolver.load_object(
            mltrain_model_name,
            config,
            kwargs={"config": config},
        )

        return mltrain_model
