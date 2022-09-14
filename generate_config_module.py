from collections import OrderedDict
import importlib
import inspect
from io import TextIOWrapper
import os.path as osp
import os
import typing
import builtins
from mmengine.utils import get_installed_path, mkdir_or_exist
from mmengine.registry import Registry
from mmengine.utils.dl_utils import register_all_modules

class Require(object):
    pass


def get_all_arguments(cls,
                      args,
                      kwonlyargs):

    if cls in builtins.__dict__.values():
        return

    full_args = inspect.getfullargspec(cls.__init__)
    _args = full_args.args
    _kwonlyargs = full_args.kwonlyargs
    _default_args = full_args.defaults
    _default_args = [] if _default_args is None else _default_args
    _default_kwargs = full_args.kwonlydefaults
    _default_kwargs = {} if _default_kwargs is None else _default_kwargs
    varkwargs = full_args.varkw
    varargs = full_args.varargs

    args_iter = iter(_args)

    for _ in range(len(_args) - len(_default_args)):
        arg = next(args_iter)
        args.setdefault(arg, Require())

    for idx in range(len(_default_args)):
        arg = next(args_iter)
        args.setdefault(arg, _default_args[idx])

    for kwonlyarg in _kwonlyargs:
        kwonlyargs.setdefault(
            kwonlyarg, _default_kwargs.get(kwonlyarg, Require()))

    if varkwargs is None and varargs is None:
        return

    for base in (cls.__base__, ):
        get_all_arguments(base, args, kwonlyargs, varargs, varkwargs)


def generate_class(cls, file=None):
    full_args = inspect.getfullargspec(cls.__init__)

    args = OrderedDict()
    default_args = OrderedDict()
    for key, value in args:
        if isinstance(value, Require):
            default_args[key] = args.pop(key)
    args.update(default_args)
    kwonlyargs= OrderedDict()
    defaults = OrderedDict()
    get_all_arguments(cls, args, kwonlyargs)
    varargs = full_args.varargs
    varkw = full_args.varkw

    class_name = cls.__name__
    module_name = cls.__module__
    indent = ' ' * 4

    def write_import(file: TextIOWrapper):
        file.write('from chanfig import Config\n')
        file.write('import typing\n')
        file.write('if typing.TYPE_CHECKING:\n')
        file.write(f'{indent}from {module_name} import {class_name}\n')

        module_name_list = module_name.split('.')
        if len(module_name_list) > 1:
            module_name_list[1] = 'config_module'
        file.write('\n')
        file.write('\n')

    def define_class(file: TextIOWrapper):
        attrs = []
        # class ClassName(Config):
        file.write(f'class {class_name}(Config):\n')
        file.write(f"{indent}type: '{class_name}'\n")
        # def __init__(self):
        file.write(f'{indent}def __init__(\n')
        args_iter = iter(args)

        for key, value in args.items():
            arg = next(args_iter)
            # annotation = get_annotation(arg)
            if isinstance(value, Require):
                file.write(f'{indent * 2}{arg}'
                           # f': {annotation}'
                           f',\n')
                attrs.append(arg)
            else:
                if isinstance(value, str):
                    value = f"'{value}'"
                file.write(f'{indent * 2}{arg}'
                           f'={value}'
                           # f': {annotation}'
                           f',\n')
                attrs.append(arg)

        if varargs is not None:
            file.write(f'{indent * 2}*{varargs},\n')

        for key, value in kwonlyargs.items():
            # annotation = get_annotation(kwonlyarg)
            # if kwonlyarg in kwonlydefaults:
            #     file.write(f'{indent * 2}{kwonlyarg}'
            #                # f': {annotation}'
            #                # f'={kwonlydefaults[kwonlyarg]}'
            #                f',\n'
            #                )
            # else:
            if isinstance(value, Require):
                file.write(f'{indent * 2}{key}'
                           # f': {annotation},'
                           f',\n')
                attrs.append(key)
            else:
                if isinstance(value, str):
                    value = f"'{value}'"
                file.write(f'{indent * 2}{key}'
                           f'={value}'
                           # f': {annotation},'
                           f',\n')
                attrs.append(key)

        if varkw is not None:
            file.write(f'{indent * 2}**{varkw}):\n')
        else:
            file.write(f'):\n')

        file.write(f'{indent * 2}super().__init__()\n')
        if len(attrs) == 1:
            file.write(f'{indent * 2}pass\n')
        else:
            for attr in attrs:
                if attr == 'self':
                    continue
                file.write(f'{indent * 2}self.{attr} = {attr}\n')

    # def get_annotation(arg):
    #     annotation = annotations.get(arg, typing.Any)
    #     try:
    #         annotation = annotation.__name__
    #         annotation
    #     except:
    #         annotation = f'typing.{annotation._name}'
    #     return annotation
    if file.tell() == 0:
        write_import(file)
    define_class(file)


def config_from_registry(registry: Registry, module_path: str):
    for name, cls in registry.module_dict.items():
        module_name = cls.__module__
        if not module_name.startswith('mmengine'):
            continue
        module_name_list = module_name.split('.')
        module_name_list = module_name_list[1:]
        module_name_list.insert(0, 'config_module')
        module_package = '/'.join(module_name_list[:-1])
        module_package = osp.join(module_path, module_package)
        module_file = '/'.join(module_name_list)
        module_file = osp.join(module_path, module_file)
        mkdir_or_exist(module_package)

        with open(module_file + '.py', 'a+') as f:
            generate_class(cls, f)


if __name__ == '__main__':
    register_all_modules()

    module_path = get_installed_path('mmengine')
    all_registry = importlib.import_module('mmengine.registry')
    config_path = osp.join(module_path, 'config_module')
    mkdir_or_exist(config_path)
    for value in all_registry.__dict__.values():
        if isinstance(value, Registry):
            config_from_registry(value, module_path)

    for root_dir, sub_dir, file in os.walk(config_path):
        with open(osp.join(root_dir, '__init__.py'), 'w') as f:
            for module in sub_dir:
                f.write(f'from .{module} import *\n')

            for module in file:
                if module.startswith('__init__'):
                    continue
                f.write(f'from .{module.rstrip(".py")} import *\n')