# Copyright (c) OpenMMLab. All rights reserved.
from abc import abstractmethod
from typing import TYPE_CHECKING, Dict, List, Union

import torch
import torch.nn as nn

from mmengine.registry import MODEL_WRAPPERS, RUNNERS
from mmengine.structures import BaseDataElement

if TYPE_CHECKING:
    from mmengine.runner import Runner

# multi-batch inputs processed by different augmentations from the same batch.
EnhancedBatchInputs = List[Union[torch.Tensor, List[torch.Tensor]]]
# multi-batch data samples processed by different augmentations from the same
# batch. The inner list stands for different augmentations and the outer list
# stands for batch.
EnhancedBatchDataSamples = List[List[BaseDataElement]]
DATA_BATCH = Union[Dict[str, Union[EnhancedBatchInputs,
                                   EnhancedBatchDataSamples]], tuple, dict]
MergedDataSamples = List[BaseDataElement]


@MODEL_WRAPPERS.register_module()
class BaseTTAModel(nn.Module):
    """Base model for inference with test-time augmentation.

    ``BaseTTAModel`` is a wrapper for inference given multi-batch data.
    It implements the :meth:`test_step` for multi-batch data inference.
    ``multi-batch`` data means data processed by different augmentation
    from the same batch.

    During test time augmentation, the data processed by
    :obj:`mmcv.transforms.TestTimeAug`, and then collated by
    ``pseudo_collate`` will have the following format:

    .. code-block::

        result = dict(
            inputs=[
                [image1_aug1, image2_aug1],
                [image1_aug2, image2_aug2]
            ],
            data_samples=[
                [data_sample1_aug1, data_sample2_aug1],
                [data_sample1_aug2, data_sample2_aug2],
            ]
        )

    ``image{i}_aug{j}`` means the i-th image of the batch, which is
    augmented by the j-th augmentation.

    ``BaseTTAModel`` will collate the data to:

     .. code-block::

        data1 = dict(
            inputs=[image1_aug1, image2_aug1],
            data_samples=[data_sample1_aug1, data_sample2_aug1]
        )

        data2 = dict(
            inputs=[image1_aug2, image2_aug2],
            data_samples=[data_sample1_aug2, data_sample2_aug2]
        )

    ``data1`` and ``data2`` will be passed to model, and the results will be
    merged by :meth:`merge_preds`.

    Note:
        :meth:`merge_preds` is an abstract method, all subclasses should
        implement it.

    Args:
        module (dict or nn.Module): Tested model.
    """

    def __init__(self, module: Union[dict, nn.Module]):
        super().__init__()
        if isinstance(module, nn.Module):
            self.module = module
        elif isinstance(module, dict):
            self.module = MODEL_WRAPPERS.build(module)
        else:
            raise TypeError('The type of module should be a `nn.Module` '
                            f'instance or a dict, but got {module}')
        assert hasattr(self.module, 'test_step'), (
            'Model wrapped by BaseTTAModel must implement `test_step`!')

    @abstractmethod
    def merge_preds(self, data_samples_list: EnhancedBatchDataSamples) \
            -> MergedDataSamples:
        """Merge predictions of enhanced data to one prediction.

        Args:
            data_samples_list (EnhancedBatchDataSamples): List of predictions
                of all enhanced data.

        Returns:
            List[BaseDataElement]: Merged prediction.
        """

    def test_step(self, data: DATA_BATCH) -> MergedDataSamples:
        """Get predictions of each enhanced data, a multiple predictions.

        Args:
            data (DataBatch): Enhanced data batch sampled from dataloader.

        Returns:
            MergedDataSamples: Merged prediction.
        """
        data_list: Union[List[dict], List[list]]
        if isinstance(data, dict):
            num_augs = len(data[next(iter(data))])
            data_list = [{key: value[idx]
                          for key, value in data.items()}
                         for idx in range(num_augs)]
        elif isinstance(data, (tuple, list)):
            num_augs = len(data[0])
            data_list = [[_data[idx] for _data in data]
                         for idx in range(num_augs)]
        else:
            raise TypeError('data given by dataLoader should be a dict, '
                            f'tuple or a list, but got {type(data)}')

        predictions = []
        for data in data_list:  # type: ignore
            predictions.append(self.module.test_step(data))
        return self.merge_preds(list(zip(*predictions)))  # type: ignore


def build_runner_with_tta(cfg) -> 'Runner':
    assert hasattr(
        cfg,
        'tta_model'), ('make please sure your config define the tta_model')
    assert hasattr(cfg, 'tta_pipeline'), (
        'make please sure your config define the tta_pipeline')
    from mmengine.hooks import PrepareTTAHook
    cfg.test_dataloader.dataset.pipeline = cfg.tta_pipeline

    if 'runner_type' in cfg:
        runner = RUNNERS.build(cfg)
    else:
        from mmengine.runner import Runner
        runner = Runner.from_cfg(cfg)

    runner.register_hook(PrepareTTAHook(tta_cfg=cfg.tta_model))
    return runner
