# Copyright (c) OpenMMLab. All rights reserved.
from abc import ABCMeta, abstractmethod
from typing import Union, Dict, List, Sequence, Optional, Callable
from collections import OrderedDict
import copy
import os.path as osp
import torch
import torch.nn as nn
import time
from torch.optim import Optimizer
import platform

from mmengine.config import Config, ConfigDict
from mmengine.registry import MODELS, PARAM_SCHEDULERS
from mmengine.utils import digit_version
from mmengine.utils.dl_utils import TORCH_VERSION, set_multi_processing
from mmengine.logging import MMLogger
from mmengine.model.wrappers import is_model_wrapper
from mmengine.model import revert_sync_batchnorm
from mmengine.dist import broadcast, get_dist_info, is_distributed
from mmengine.optim import (OptimWrapper, OptimWrapperDict, _ParamScheduler,
                            build_optim_wrapper)


ParamSchedulerType = Union[List[_ParamScheduler], Dict[str,
                                                       List[_ParamScheduler]]]


class BaseStrategy(metaclass=ABCMeta):
    """Base class for all strategies.
    
    Args:
        compile (dict or bool): Config to compile model. Defaults to False.
    """

    def __init__(self,
                 *,
                 compile: Union[dict, bool] = False,
                 **kwargs,
                 ):
        self.compile = compile

    @abstractmethod
    def prepare(self,
                model: Union[nn.Module, dict],
                *,
                optim_wrapper: Optional[Union[OptimWrapper, dict]] = None,
                param_scheduler: Optional[Union[_ParamScheduler, Dict, List]] = None,
                compile_target: str = 'forward',
                checkpoint: Optional[dict] = None,
                num_batches_per_epoch: Optional[int] = None,
                max_epochs: Optional[int] = None,
                max_iters: Optional[int] = None,
                cur_iter: Optional[int] = None,
                **kwargs):
        """Prepare model and some components.
        
        Args:
            model (:obj:`torch.nn.Module` or dict): The model to be run. It can be
                a dict used for build a model.

        Kwargs:
            optim_wrapper (OptimWrapper or dict, optional):
                Computing gradient of model parameters. If specified,
                :attr:`train_dataloader` should also be specified. If automatic
                mixed precision or gradient accmulation
                training is required. The type of ``optim_wrapper`` should be
                AmpOptimizerWrapper. See :meth:`build_optim_wrapper` for
                examples. Defaults to None.
            param_scheduler (_ParamScheduler or dict or list, optional):
                Parameter scheduler for updating optimizer parameters. If
                specified, :attr:`optimizer` should also be specified.
                Defaults to None.
                See :meth:`build_param_scheduler` for examples.
            compile_target (str): The method of model to be compiled.
                Defaults to 'forward'.
            checkpoint (dict, optional): Checkpoint to load strategy state.
                Defaults to None.
            num_batches_per_epoch (int, optional): Number of batches per epoch.
                Defaults to None.
            max_epochs (int, optional): Number of epochs. Defaults to None.
            max_iters (int, optional): Number of iterations. Defaults to None.
            cur_iter (int, optional): Current iteration. Defaults to None.
        """

    def setup_env(self,
                  *,
                  launcher: str = 'none',
                  distributed: bool = False,
                  cudnn_benchmark: bool = False,
                  mp_cfg: Optional[dict] = None,
                  dist_cfg: Optional[dict] = None,
                  resource_limit: int = 4096,
                  randomness: dict = dict(seed=None)):
        """Setup environment.
        
        1. setup multi-processing
        2. setup distributed
        3. set random seed
        """
        if cudnn_benchmark:
            torch.backends.cudnn.benchmark = True

        mp_cfg = mp_cfg if mp_cfg is not None else {}
        set_multi_processing(**mp_cfg, distributed=distributed)

        # init distributed env first, since logger depends on the dist info.
        if distributed and not is_distributed():
            dist_cfg = dist_cfg if dist_cfg is not None else {}
            self.setup_distributed(launcher, **dist_cfg)

        self._rank, self._world_size = get_dist_info()

        timestamp = torch.tensor(time.time(), dtype=torch.float64)
        # broadcast timestamp from 0 process to other processes
        broadcast(timestamp)
        self._timestamp = time.strftime('%Y%m%d_%H%M%S',
                                        time.localtime(timestamp.item()))

        # https://github.com/pytorch/pytorch/issues/973
        # set resource limit
        if platform.system() != 'Windows':
            import resource
            rlimit = resource.getrlimit(resource.RLIMIT_NOFILE)
            base_soft_limit = rlimit[0]
            hard_limit = rlimit[1]
            soft_limit = min(max(resource_limit, base_soft_limit), hard_limit)
            resource.setrlimit(resource.RLIMIT_NOFILE,
                               (soft_limit, hard_limit))

        self.set_randomness(**randomness)

    def set_randomness(self,
                       seed,
                       diff_rank_seed: bool = False,
                       deterministic: bool = False) -> None:
        """Set random seed to guarantee reproducible results.

        Args:
            seed (int): A number to set random modules.
            diff_rank_seed (bool): Whether or not set different seeds according
                to global rank. Defaults to False.
            deterministic (bool): Whether to set the deterministic option for
                CUDNN backend, i.e., set `torch.backends.cudnn.deterministic`
                to True and `torch.backends.cudnn.benchmark` to False.
                Defaults to False.
                See https://pytorch.org/docs/stable/notes/randomness.html for
                more details.
        """
        from mmengine.runner import set_random_seed
        self._seed = set_random_seed(
            seed=seed,
            deterministic=deterministic,
            diff_rank_seed=diff_rank_seed)

    def setup_distributed(self, *args, **kwargs):
        """Setup distributed training."""
        pass

    def build_model(self, model: Union[nn.Module, dict]) -> nn.Module:
        """Build model.

        If ``model`` is a dict, it will be used to build a nn.Module object.
        Otherwise, if ``model`` is a nn.Module object it will be returned
        directly.

        An example of ``model``::

            model = dict(type='ResNet')

        Args:
            model (nn.Module or dict): A ``nn.Module`` object or a dict to
                build nn.Module object. If ``model`` is a nn.Module object,
                just returns itself.

        Note:
            The returned model must implement ``train_step``, ``test_step``
            if ``runner.train`` or ``runner.test`` will be called. If
            ``runner.val`` will be called or ``val_cfg`` is configured,
            model must implement `val_step`.

        Returns:
            nn.Module: Model build from ``model``.
        """
        if isinstance(model, nn.Module):
            return model
        elif isinstance(model, dict):
            model = MODELS.build(model)
            return model  # type: ignore
        else:
            raise TypeError('model should be a nn.Module object or dict, '
                            f'but got {model}')

    def convert_model(self, model: nn.Module) -> nn.Module:
        """Convert layers of model.
        
        convert all `SyncBatchNorm` (SyncBN) and
        `mmcv.ops.sync_bn.SyncBatchNorm`(MMSyncBN) layers in the model to
        `BatchNormXd` layers.

        Args:
            model (nn.Module): Model to convert.
        """
        self.logger.info(
            'Distributed training is not used, all SyncBatchNorm (SyncBN) '
            'layers in the model will be automatically reverted to '
            'BatchNormXd layers if they are used.')
        model = revert_sync_batchnorm(model)
        return model

    def compile_model(self, model: nn.Module, target: str = 'forward') -> nn.Module:
        """Compile model.

        Args:
            model (nn.Module): Model to compile.

        Returns:
            nn.Module: Compiled model.
        """
        if not self.compile:
            return model

        assert digit_version(TORCH_VERSION) >= digit_version('2.0.0'), (
            'PyTorch >= 2.0.0 is required to enable torch.compile')

        compile = dict() if isinstance(self.compile, bool) else self.compile
        target = compile.pop('target', target)
        func = getattr(model, target)
        compiled_func = torch.compile(func, **compile)
        setattr(model, target, compiled_func)
        self.logger.info('Model has been "compiled". The first few iterations '
                         'will be slow, please be patient.')
        
        return model

    def wrap_model(self, model: nn.Module) -> nn.Module:
        """Wrap model.

        Args:
            model (nn.Module): Model to wrap.

        Returns:
            nn.Module: Wrapped model.
        """
        return model

    def _init_model_weights(self, model: nn.Module) -> nn.Module:
        """Initialize the model weights if the model has
        :meth:`init_weights`"""
        if hasattr(model, 'init_weights'):
            model.init_weights()
            # sync params and buffers
            for _, params in model.state_dict().items():
                broadcast(params)
        
        return model

    def build_optim_wrapper(
        self, optim_wrapper: Union[Optimizer, OptimWrapper, dict]
    ) -> Union[OptimWrapper, OptimWrapperDict]:
        """Build optimizer wrapper.

        If ``optim_wrapper`` is a config dict for only one optimizer,
        the keys must contain ``optimizer``, and ``type`` is optional.
        It will build a :obj:`OptimWrapper` by default.

        If ``optim_wrapper`` is a config dict for multiple optimizers, i.e.,
        it has multiple keys and each key is for an optimizer wrapper. The
        constructor must be specified since
        :obj:`DefaultOptimizerConstructor` cannot handle the building of
        training with multiple optimizers.

        If ``optim_wrapper`` is a dict of pre-built optimizer wrappers, i.e.,
        each value of ``optim_wrapper`` represents an ``OptimWrapper``
        instance. ``build_optim_wrapper`` will directly build the
        :obj:`OptimWrapperDict` instance from ``optim_wrapper``.

        Args:
            optim_wrapper (OptimWrapper or dict): An OptimWrapper object or a
                dict to build OptimWrapper objects. If ``optim_wrapper`` is an
                OptimWrapper, just return an ``OptimizeWrapper`` instance.

        Note:
            For single optimizer training, if `optim_wrapper` is a config
            dict, `type` is optional(defaults to :obj:`OptimWrapper`) and it
            must contain `optimizer` to build the corresponding optimizer.

        Examples:
            >>> # build an optimizer
            >>> optim_wrapper_cfg = dict(type='OptimWrapper', optimizer=dict(
            ...     type='SGD', lr=0.01))
            >>> # optim_wrapper_cfg = dict(optimizer=dict(type='SGD', lr=0.01))
            >>> # is also valid.
            >>> optim_wrapper = runner.build_optim_wrapper(optim_wrapper_cfg)
            >>> optim_wrapper
            Type: OptimWrapper
            accumulative_counts: 1
            optimizer:
            SGD (
            Parameter Group 0
                dampening: 0
                lr: 0.01
                momentum: 0
                nesterov: False
                weight_decay: 0
            )
            >>> # build optimizer without `type`
            >>> optim_wrapper_cfg = dict(optimizer=dict(type='SGD', lr=0.01))
            >>> optim_wrapper = runner.build_optim_wrapper(optim_wrapper_cfg)
            >>> optim_wrapper
            Type: OptimWrapper
            accumulative_counts: 1
            optimizer:
            SGD (
            Parameter Group 0
                dampening: 0
                lr: 0.01
                maximize: False
                momentum: 0
                nesterov: False
                weight_decay: 0
            )
            >>> # build multiple optimizers
            >>> optim_wrapper_cfg = dict(
            ...    generator=dict(type='OptimWrapper', optimizer=dict(
            ...        type='SGD', lr=0.01)),
            ...    discriminator=dict(type='OptimWrapper', optimizer=dict(
            ...        type='Adam', lr=0.001))
            ...    # need to customize a multiple optimizer constructor
            ...    constructor='CustomMultiOptimizerConstructor',
            ...)
            >>> optim_wrapper = runner.optim_wrapper(optim_wrapper_cfg)
            >>> optim_wrapper
            name: generator
            Type: OptimWrapper
            accumulative_counts: 1
            optimizer:
            SGD (
            Parameter Group 0
                dampening: 0
                lr: 0.1
                momentum: 0
                nesterov: False
                weight_decay: 0
            )
            name: discriminator
            Type: OptimWrapper
            accumulative_counts: 1
            optimizer:
            'discriminator': Adam (
            Parameter Group 0
                dampening: 0
                lr: 0.02
                momentum: 0
                nesterov: False
                weight_decay: 0
            )

        Important:
            If you need to build multiple optimizers, you should implement a
            MultiOptimWrapperConstructor which gets parameters passed to
            corresponding optimizers and compose the ``OptimWrapperDict``.
            More details about how to customize OptimizerConstructor can be
            found at `optimizer-docs`_.

        Returns:
            OptimWrapper: Optimizer wrapper build from ``optimizer_cfg``.
        """  # noqa: E501
        if isinstance(optim_wrapper, OptimWrapper):
            return optim_wrapper
        if isinstance(optim_wrapper, (dict, ConfigDict, Config)):
            # optimizer must be defined for single optimizer training.
            optimizer = optim_wrapper.get('optimizer', None)

            # If optimizer is a built `Optimizer` instance, the optimizer
            # wrapper should be built by `OPTIM_WRAPPERS` registry.
            if isinstance(optimizer, Optimizer):
                optim_wrapper.setdefault('type', 'OptimWrapper')
                return OPTIM_WRAPPERS.build(optim_wrapper)  # type: ignore

            # If `optimizer` is not None or `constructor` is defined, it means,
            # optimizer wrapper will be built by optimizer wrapper
            # constructor. Therefore, `build_optim_wrapper` should be called.
            if optimizer is not None or 'constructor' in optim_wrapper:
                return build_optim_wrapper(self.model, optim_wrapper)
            else:
                # if `optimizer` is not defined, it should be the case of
                # training with multiple optimizers. If `constructor` is not
                # defined either, each value of `optim_wrapper` must be an
                # `OptimWrapper` instance since `DefaultOptimizerConstructor`
                # will not handle the case of training with multiple
                # optimizers. `build_optim_wrapper` will directly build the
                # `OptimWrapperDict` instance from `optim_wrapper.`
                optim_wrappers = OrderedDict()
                for name, optim in optim_wrapper.items():
                    if not isinstance(optim, OptimWrapper):
                        raise ValueError(
                            'each item mush be an optimizer object when '
                            '"type" and "constructor" are not in '
                            f'optimizer, but got {name}={optim}')
                    optim_wrappers[name] = optim
                return OptimWrapperDict(**optim_wrappers)
        else:
            raise TypeError('optimizer wrapper should be an OptimWrapper '
                            f'object or dict, but got {optim_wrapper}')

    def _build_param_scheduler(
            self, scheduler: Union[_ParamScheduler, Dict, List],
            optim_wrapper: OptimWrapper,
            default_args: dict) -> List[_ParamScheduler]:
        """Build parameter schedulers for a single optimizer.

        Args:
            scheduler (_ParamScheduler or dict or list): A Param Scheduler
                object or a dict or list of dict to build parameter schedulers.
            optim_wrapper (OptimWrapper): An optimizer wrapper object is
                passed to construct ParamScheduler object.

        Returns:
            list[_ParamScheduler]: List of parameter schedulers build from
            ``scheduler``.

        Note:
            If the train loop is built, when building parameter schedulers,
            it supports setting the max epochs/iters as the default ``end``
            of schedulers, and supports converting epoch-based schedulers
            to iter-based according to the ``convert_to_iter_based`` key.
        """
        if not isinstance(scheduler, Sequence):
            schedulers = [scheduler]
        else:
            schedulers = scheduler

        max_epochs = default_args.pop('max_epochs', None)
        max_iters = default_args.pop('max_iters', None)

        param_schedulers = []
        for scheduler in schedulers:
            if isinstance(scheduler, _ParamScheduler):
                param_schedulers.append(scheduler)
            elif isinstance(scheduler, dict):
                _scheduler = copy.deepcopy(scheduler)

                # Set default end
                if _scheduler.get('by_epoch', True):
                    if max_epochs is None:
                        raise ValueError(
                            'max_epochs must be specified in default_args')
                    default_end = max_epochs
                else:
                    if max_iters is None:
                        raise ValueError(
                            'max_iters must be specified in default_args')
                    default_end = max_iters
                _scheduler.setdefault('end', default_end)
                self.logger.debug(
                    f'The `end` of {_scheduler["type"]} is not set. '
                    'Use the max epochs/iters of train loop as default.')

                param_schedulers.append(
                    PARAM_SCHEDULERS.build(
                        _scheduler,
                        default_args=dict(optimizer=optim_wrapper, **default_args)))
            else:
                raise TypeError(
                    'scheduler should be a _ParamScheduler object or dict, '
                    f'but got {scheduler}')
        return param_schedulers

    def build_param_scheduler(
            self, scheduler: Union[_ParamScheduler, Dict,
                                   List], default_args) -> ParamSchedulerType:
        """Build parameter schedulers.

        ``build_param_scheduler`` should be called after
        ``build_optim_wrapper`` because the building logic will change
        according to the number of optimizers built by the runner.
        The cases are as below:

        - Single optimizer: When only one optimizer is built and used in the
          runner, ``build_param_scheduler`` will return a list of
          parameter schedulers.
        - Multiple optimizers: When two or more optimizers are built and used
          in runner, ``build_param_scheduler`` will return a dict containing
          the same keys with multiple optimizers and each value is a list of
          parameter schedulers. Note that, if you want different optimizers to
          use different parameter schedulers to update optimizer's
          hyper-parameters, the input parameter ``scheduler`` also needs to be
          a dict and its key are consistent with multiple optimizers.
          Otherwise, the same parameter schedulers will be used to update
          optimizer's hyper-parameters.

        Args:
            scheduler (_ParamScheduler or dict or list): A Param Scheduler
                object or a dict or list of dict to build parameter schedulers.

        Examples:
            >>> # build one scheduler
            >>> optim_cfg = dict(dict(type='SGD', lr=0.01))
            >>> runner.optim_wrapper = runner.build_optim_wrapper(
            >>>     optim_cfg)
            >>> scheduler_cfg = dict(type='MultiStepLR', milestones=[1, 2])
            >>> schedulers = runner.build_param_scheduler(scheduler_cfg)
            >>> schedulers
            [<mmengine.optim.scheduler.lr_scheduler.MultiStepLR at 0x7f70f6966290>]  # noqa: E501

            >>> # build multiple schedulers
            >>> scheduler_cfg = [
            ...    dict(type='MultiStepLR', milestones=[1, 2]),
            ...    dict(type='StepLR', step_size=1)
            ... ]
            >>> schedulers = runner.build_param_scheduler(scheduler_cfg)
            >>> schedulers
            [<mmengine.optim.scheduler.lr_scheduler.MultiStepLR at 0x7f70f60dd3d0>,  # noqa: E501
            <mmengine.optim.scheduler.lr_scheduler.StepLR at 0x7f70f6eb6150>]

        Above examples only provide the case of one optimizer and one scheduler
        or multiple schedulers. If you want to know how to set parameter
        scheduler when using multiple optimizers, you can find more examples
        `optimizer-docs`_.

        Returns:
            list[_ParamScheduler] or dict[str, list[_ParamScheduler]]: List of
            parameter schedulers or a dictionary contains list of parameter
            schedulers build from ``scheduler``.

        .. _optimizer-docs:
           https://mmengine.readthedocs.io/en/latest/tutorials/optim_wrapper.html
        """
        param_schedulers: ParamSchedulerType
        if not isinstance(self.optim_wrapper, OptimWrapperDict):
            # Since `OptimWrapperDict` inherits from `OptimWrapper`,
            # `isinstance(self.optim_wrapper, OptimWrapper)` cannot tell
            # whether `self.optim_wrapper` is an `OptimizerWrapper` or
            # `OptimWrapperDict` instance. Therefore, here we simply check
            # self.optim_wrapper is not an `OptimWrapperDict` instance and
            # then assert it is an OptimWrapper instance.
            assert isinstance(self.optim_wrapper, OptimWrapper), (
                '`build_optimizer` should be called before'
                '`build_param_scheduler` because the latter depends '
                'on the former')
            param_schedulers = self._build_param_scheduler(
                scheduler, self.optim_wrapper, default_args)  # type: ignore
            return param_schedulers
        else:
            param_schedulers = dict()
            for name, optimizer in self.optim_wrapper.items():
                if isinstance(scheduler, dict) and 'type' not in scheduler:
                    # scheduler is a dict and each item is a ParamScheduler
                    # object or a config to build ParamScheduler objects
                    param_schedulers[name] = self._build_param_scheduler(
                        scheduler[name], optimizer, default_args)
                else:
                    param_schedulers[name] = self._build_param_scheduler(
                        scheduler, optimizer, default_args)

            return param_schedulers

    def build_logger(self,
                     log_level: Union[int, str] = 'INFO',
                     log_dir: Optional[str] = None,
                     log_file: Optional[str] = None,
                     exp_name: Optional[str] = None,
                     **kwargs) -> MMLogger:
        """Build a global asscessable MMLogger.

        Args:
            log_level (int or str): The log level of MMLogger handlers.
                Defaults to 'INFO'.
            log_file (str, optional): Path of filename to save log.
                Defaults to None.
            **kwargs: Remaining parameters passed to ``MMLogger``.

        Returns:
            MMLogger: A MMLogger object build from ``logger``.
        """
        if log_file is None:
            log_file = osp.join(log_dir, f'{self._timestamp}.log')

        log_cfg = dict(log_level=log_level, log_file=log_file, **kwargs)
        log_cfg.setdefault('name', exp_name)
        # `torch.compile` in PyTorch 2.0 could close all user defined handlers
        # unexpectedly. Using file mode 'a' can help prevent abnormal
        # termination of the FileHandler and ensure that the log file could
        # be continuously updated during the lifespan of the runner.
        log_cfg.setdefault('file_mode', 'a')
        self.logger = MMLogger.get_instance(**log_cfg)  # type: ignore

        return self.logger

    def state_dict(self,
                   *,
                   save_optimizer: bool = True,
                   save_param_scheduler: bool = True) -> dict:
        """Get the state of strategy.
        
        The state of strategy contains the following items:
        - state_dict: The state dict of model.
        - optimizer: The state dict of optimizer.
        - param_schedulers: The state dict of parameter scheduler.

        Args:
            save_optimizer (bool): Whether to save the optimizer to
                the checkpoint. Defaults to True.
            save_param_scheduler (bool): Whether to save the param_scheduler
                to the checkpoint. Defaults to True.
        
        Returns:
            dict: The state of strategy.
        """
        state_dict = dict()
        state_dict['state_dict'] = self.model_state_dict()

        # save optimizer state dict
        if save_optimizer and hasattr(self, 'optim_wrapper'):
            state_dict['optimizer'] = self.optim_state_dict()
        
        # save param scheduler state dict
        if save_param_scheduler and not hasattr(self, 'param_schedulers'):
            self.logger.warning(
                '`save_param_scheduler` is True but strategy has no param_schedulers '
                'attribute, so skip saving parameter schedulers')
            save_param_scheduler = False

        if save_param_scheduler:
            state_dict['param_schedulers'] = self.scheduler_state_dict()
        
        return state_dict

    def model_state_dict(self) -> dict:
        """Get model state dict."""
        from mmengine.runner import get_state_dict, weights_to_cpu
        return weights_to_cpu(get_state_dict(self.model))

    def optim_state_dict(self) -> dict:
        """Get optimizer state dict."""
        if isinstance(self.optim_wrapper, OptimWrapper):
            return self.optim_wrapper.state_dict()
        else:
            raise TypeError(
                'self.optim_wrapper should be an `OptimWrapper` '
                'or `OptimWrapperDict` instance, but got '
                f'{self.optim_wrapper}')

    def scheduler_state_dict(self) -> Union[dict, list]:
        """Get parameter scheduler state dict."""
        if isinstance(self.param_schedulers, dict):
            state_dict = dict()
            for name, schedulers in self.param_schedulers.items():
                state_dict[name] = []
                for scheduler in schedulers:
                    state_dict[name].append(scheduler.state_dict())
            return state_dict
        else:
            state_list = []
            for scheduler in self.param_schedulers:  # type: ignore
                state_list.append(scheduler.state_dict())
            return state_list

    def load_state_dict(self, state_dict: dict, strict: bool = False, revise_keys: list = [(r'^module.', '')]) -> dict:
        """Load strategy state."""
        self.load_model_state_dict(state_dict, strict=strict, revise_keys=revise_keys)

        # resume optimizer
        if 'optimizer' in state_dict:
            self.load_optim_state_dict(state_dict)
        
        # resume param scheduler
        if 'param_schedulers' in state_dict:
            self.load_param_scheduler(state_dict)

    def load_model_state_dict(self,
                              state_dict: dict,
                              *,
                              strict: bool = False,
                              revise_keys: list = [(r'^module.', '')]) -> None:
        """Load model state from dict."""
        from mmengine.runner.checkpoint import _load_checkpoint_to_model

        assert 'state_dict' in state_dict
        if is_model_wrapper(self.model):
            model = self.model.module
        else:
            model = self.model

        _load_checkpoint_to_model(model, state_dict['state_dict'], strict, revise_keys)


    def load_optim_state_dict(self, state_dict: dict) -> None:
        """Load optimizer state from dict."""
        assert 'optimizer' in state_dict
        self.optim_wrapper.load_state_dict(state_dict['optimizer'])

    def load_scheduler_state_dict(self, state_dict: dict) -> None:
        """Load scheduler state from dict."""
        assert 'param_schedulers' in state_dict
        if isinstance(self.param_schedulers, dict):
            for name, schedulers in self.param_schedulers.items():
                for scheduler, ckpt_scheduler in zip(
                        schedulers, state_dict['param_schedulers'][name]):
                    scheduler.load_state_dict(ckpt_scheduler)
        else:
            for scheduler, ckpt_scheduler in zip(
                    self.param_schedulers,  # type: ignore
                    state_dict['param_schedulers']):
                scheduler.load_state_dict(ckpt_scheduler)

    def load_checkpoint(self,
                        filename: str,
                        map_location: Union[str, Callable] = 'cpu',
                        *,
                        callback: Optional[Callable] = None) -> dict:
        """Load checkpoint from given ``filename``.

        Args:
            filename (str): Accept local filepath, URL, ``torchvision://xxx``,
                ``open-mmlab://xxx``.
            map_location (str or callable): A string or a callable function to
                specifying how to remap storage locations.
                Defaults to 'cpu'.
        """
        from mmengine.runner.checkpoint import _load_checkpoint

        checkpoint = _load_checkpoint(filename, map_location=map_location)

        # users can do some modification after loading checkpoint
        if callback is not None:
            callback(checkpoint)

        return checkpoint

    def save_checkpoint(self,
                        filename: str,
                        *,
                        save_optimizer: bool = True,
                        save_param_scheduler: bool = True,
                        extra_ckpt: Optional[dict] = None,
                        callback: Optional[Callable] = None) -> None:
        """Save checkpoint to given ``filename``.

        Args:
            filename (str): Filename to save checkpoint.
            save_optimizer (bool): Whether to save the optimizer to
                the checkpoint. Defaults to True.
            save_param_scheduler (bool): Whether to save the param_scheduler
                to the checkpoint. Defaults to True.
            extra_ckpt (dict): Extra checkpoint to save. Defaults to None.
            callback (callable): Callback function to modify the checkpoint.
                Defaults to None.
            """
        from mmengine.runner.checkpoint import save_checkpoint

        state_dict = self.state_dict(save_optimizer=save_optimizer,
                                     save_param_scheduler=save_param_scheduler)

        # save extra checkpoint passed by users
        if extra_ckpt is not None:
            state_dict.update(extra_ckpt)
        
        # users can do some modification before saving checkpoint
        if callback is not None:
            callback(state_dict)

        save_checkpoint(state_dict, filename)
