# Copyright (c) OpenMMLab. All rights reserved.
import torch
from typing import Any, Dict, Union
from mmengine.registry import MODEL_WRAPPERS
from mmengine.optim import OptimWrapper
from deepspeed.runtime.engine import DeepSpeedEngine

@MODEL_WRAPPERS.register_module()
class MMDeepSpeedEngine(DeepSpeedEngine):
    
    def __init__(
        self,
        args=None,
        model=None,
        optimizer=None,
        model_parameters=None,
        training_data=None,
        lr_scheduler=None,
        mpu=None,
        dist_init_required=None,
        collate_fn=None,
        config=None,
        dont_change_device=False,
    ):
        if config is None:
            config = dict()

        super().__init__(args=args,
                         model=model,
                         optimizer=optimizer,
                         model_parameters=model_parameters,
                         training_data=training_data,
                         lr_scheduler=lr_scheduler,
                         mpu=mpu,
                         dist_init_required=dist_init_required,
                         collate_fn=collate_fn,
                         config=config,
                         dont_change_device=dont_change_device)

    def train_step(self, data: Union[dict, tuple, list],
                   optim_wrapper: OptimWrapper) -> Dict[str, torch.Tensor]:
        data = self.module.data_preprocessor(data, training=True)
        losses = self._run_forward(data, mode='loss')
        parsed_loss, log_vars = self.module.parse_losses(losses)
        optim_wrapper.update_params(parsed_loss, model=self)

        return log_vars

    def val_step(self, data: Union[dict, tuple, list]) -> list:
        """Gets the prediction of module during validation process.

        Args:
            data (dict or tuple or list): Data sampled from dataset.

        Returns:
            list: The predictions of given data.
        """
        return self.module.val_step(data)

    def test_step(self, data: Union[dict, tuple, list]) -> list:
        """Gets the predictions of module during testing process.

        Args:
            data (dict or tuple or list): Data sampled from dataset.

        Returns:
            list: The predictions of given data.
        """
        return self.module.test_step(data)

    def _run_forward(self, data: Union[dict, tuple, list], mode: str) -> Any:
        """Unpacks data for :meth:`forward`

        Args:
            data (dict or tuple or list): Data sampled from dataset.
            mode (str): Mode of forward.

        Returns:
            dict or list: Results of training or testing mode.
        """
        if isinstance(data, dict):
            results = self(**data, mode=mode)
        elif isinstance(data, (list, tuple)):
            results = self(*data, mode=mode)
        else:
            raise TypeError('Output of `data_preprocessor` should be '
                            f'list, tuple or dict, but got {type(data)}')
        return results
