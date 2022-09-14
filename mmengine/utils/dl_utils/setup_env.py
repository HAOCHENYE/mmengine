# Copyright (c) OpenMMLab. All rights reserved.
import datetime
import os
import platform
import warnings

import torch.multiprocessing as mp

from mmengine.registry import DefaultScope


def set_multi_processing(mp_start_method: str = 'fork',
                         opencv_num_threads: int = 0,
                         distributed: bool = False) -> None:
    """Set multi-processing related environment.

    Args:
        mp_start_method (str): Set the method which should be used to start
            child processes. Defaults to 'fork'.
        opencv_num_threads (int): Number of threads for opencv.
            Defaults to 0.
        distributed (bool): True if distributed environment.
            Defaults to False.
    """
    # set multi-process start method as `fork` to speed up the training
    if platform.system() != 'Windows':
        current_method = mp.get_start_method(allow_none=True)
        if (current_method is not None and current_method != mp_start_method):
            warnings.warn(
                f'Multi-processing start method `{mp_start_method}` is '
                f'different from the previous setting `{current_method}`.'
                f'It will be force set to `{mp_start_method}`. You can '
                'change this behavior by changing `mp_start_method` in '
                'your config.')
        mp.set_start_method(mp_start_method, force=True)

    try:
        import cv2

        # disable opencv multithreading to avoid system being overloaded
        cv2.setNumThreads(opencv_num_threads)
    except ImportError:
        pass

    # setup OMP threads
    # This code is referred from https://github.com/pytorch/pytorch/blob/master/torch/distributed/run.py  # noqa
    if 'OMP_NUM_THREADS' not in os.environ and distributed:
        omp_num_threads = 1
        warnings.warn(
            'Setting OMP_NUM_THREADS environment variable for each process'
            f' to be {omp_num_threads} in default, to avoid your system '
            'being overloaded, please further tune the variable for '
            'optimal performance in your application as needed.')
        os.environ['OMP_NUM_THREADS'] = str(omp_num_threads)

    # setup MKL threads
    if 'MKL_NUM_THREADS' not in os.environ and distributed:
        mkl_num_threads = 1
        warnings.warn(
            'Setting MKL_NUM_THREADS environment variable for each process'
            f' to be {mkl_num_threads} in default, to avoid your system '
            'being overloaded, please further tune the variable for '
            'optimal performance in your application as needed.')
        os.environ['MKL_NUM_THREADS'] = str(mkl_num_threads)


def register_all_modules(init_default_scope: bool = True) -> None:
    """Register all modules in mmengine into the registries.

    Args:
        init_default_scope (bool): Whether initialize the mmengine default scope.
            When `init_default_scope=True`, the global default scope will be
            set to `mmengine`, and all registries will build modules from mmengine's
            registry node. To understand more about the registry, please refer
            to https://github.com/open-mmlab/mmengine/blob/main/docs/en/tutorials/registry.md
            Defaults to True.
    """  # noqa
    import mmengine.dataset  # noqa: F401,F403
    import mmengine.evaluator  # noqa: F401,F403
    import mmengine.hooks  # noqa: F401,F403
    import mmengine.model  # noqa: F401,F403
    import mmengine.optim  # noqa: F401,F403
    import mmengine.runner  # noqa: F401,F403
    import mmengine.visualization  # noqa: F401,F403

    if init_default_scope:
        never_created = DefaultScope.get_current_instance() is None \
                        or not DefaultScope.check_instance_created('mmengine')
        if never_created:
            DefaultScope.get_instance('mmengine', scope_name='mmengine')
            return
        current_scope = DefaultScope.get_current_instance()
        if current_scope.scope_name != 'mmengine':
            warnings.warn('The current default scope '
                          f'"{current_scope.scope_name}" is not "mmengine", '
                          '`register_all_modules` will force the current'
                          'default scope to be "mmengine". If this is not '
                          'expected, please set `init_default_scope=False`.')
            # avoid name conflict
            new_instance_name = f'mmengine-{datetime.datetime.now()}'
            DefaultScope.get_instance(new_instance_name, scope_name='mmengine')
