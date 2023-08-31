# Copyright (c) 2017-2023, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# pylint: disable=no-member
import sys
import threading
import warnings
from itertools import count

import nvidia.dali.python_function_plugin
from nvidia.dali import backend as _b
from nvidia.dali import fn as _functional
from nvidia.dali import internal as _internal
from nvidia.dali.data_node import DataNode as _DataNode
from nvidia.dali.pipeline import Pipeline as _Pipeline
from nvidia.dali.types import (_type_name_convert_to_string, _type_convert_value,  # noqa: F401
                               _default_converter, _vector_element_type, _bool_types,  # noqa: F401
                               _int_like_types, _float_types, DALIDataType, CUDAStream as
                               _CUDAStream, ScalarConstant as _ScalarConstant, Constant as
                               _Constant)  # noqa: F401
from nvidia.dali import _conditionals

from nvidia.dali.ops import (_registry, _names, _docs)  # noqa: F401

# reexpose what was previously visible:
from nvidia.dali.ops._registry import (cpu_ops, mixed_ops, gpu_ops, register_cpu_op,  # noqa: F401
                                       register_gpu_op)  # noqa: F401
from nvidia.dali.ops._names import (_op_name, _process_op_name, _schema_name)

cupy = None


def _setup_cupy():
    global cupy
    if cupy is None:
        import cupy as cupy


class _OpCounter(object):
    # pylint: disable=too-few-public-methods
    _lock = threading.Lock()
    _op_count = count(0)

    def __init__(self):
        with self._lock:
            self._id = next(self._op_count)

    @property
    def id(self):
        return self._id


def _instantiate_constant_node(device, constant):
    return _Constant(device=device, value=constant.value, dtype=constant.dtype,
                     shape=constant.shape)


def _separate_kwargs(kwargs, arg_input_type=_DataNode):
    """Separates arguments into ones that should go to operator's __init__ and to __call__.

    Returns a pair of dictionaries of kwargs - the first for __init__, the second for __call__.

    Args:
        kwargs: Keyword arguments.
        arg_input_type: operator's argument input type, DataNode for pipeline mode, TensorListCPU
            for eager mode.
    """

    def is_arg_input_type(x):
        return isinstance(x, arg_input_type)

    def is_call_arg(name, value):
        if name == "device":
            return False
        if name == "ndim":
            return False
        if name == "name" or is_arg_input_type(value):
            return True
        if isinstance(value, (str, list, tuple, nvidia.dali.types.ScalarConstant)):
            return False
        return not nvidia.dali.types._is_scalar_value(value)

    def to_scalar(scalar):
        return scalar.value if isinstance(scalar, nvidia.dali.types.ScalarConstant) else scalar

    init_args = {}
    call_args = {}
    for name, value in kwargs.items():
        if value is None:
            continue
        if is_call_arg(name, value):
            call_args[name] = value
        else:
            init_args[name] = to_scalar(value)

    return init_args, call_args


def _add_spec_args(schema, spec, kwargs):
    for key, value in kwargs.items():
        if value is None:
            # None is not a valid value for any argument type, so treat it
            # as if the argument was not supplied at all
            continue

        dtype = schema.GetArgumentType(key)
        if isinstance(value, (list, tuple)):
            if len(value) == 0:
                spec.AddArgEmptyList(key, _vector_element_type(dtype))
                continue
        converted_value = _type_convert_value(dtype, value)
        spec.AddArg(key, converted_value)


class _OperatorInstance(object):

    def __init__(self, inputs, op, **kwargs):
        self._counter = _OpCounter()
        self._outputs = []
        self._op = op
        self._default_call_args = op._call_args
        self._spec = op.spec.copy()
        self._relation_id = self._counter.id

        if inputs is not None:
            default_input_device = "gpu" if op.device == "gpu" else "cpu"
            inputs = list(inputs)
            for i in range(len(inputs)):
                inp = inputs[i]
                if isinstance(inp, _ScalarConstant):
                    inputs[i] = _instantiate_constant_node(default_input_device, inp)
            inputs = tuple(inputs)

        if _conditionals.conditionals_enabled():
            inputs, kwargs = _conditionals.apply_conditional_split_to_args(inputs, kwargs)

        self._inputs = inputs

        spec_args, kwargs = _separate_kwargs(kwargs)
        _add_spec_args(op._schema, self._spec, spec_args)

        call_args = {**self._default_call_args}
        for k, v in kwargs.items():
            if v is None:
                # if an argument was specified in __init__ and in __call__ it is None, ignore it
                continue
            if k in self._default_call_args:
                raise ValueError("The argument `{}` was already specified in __init__.".format(k))
            call_args[k] = v

        name = call_args.get("name", None)
        if name is not None:
            self._name = name
        else:
            self._name = '__' + type(op).__name__ + "_" + str(self._counter.id)
        # Add inputs
        if inputs:
            for inp in inputs:
                if not isinstance(inp, _DataNode):
                    raise TypeError(
                        f"Expected inputs of type `DataNode`. Received input of type '{inp}'.")
                self._spec.AddInput(inp.name, inp.device)
        # Argument inputs
        for k in sorted(call_args.keys()):
            if k not in ["name"]:
                arg_inp = call_args[k]
                if arg_inp is None:
                    continue
                if isinstance(arg_inp, _ScalarConstant):
                    arg_inp = _instantiate_constant_node("cpu", arg_inp)
                if not isinstance(arg_inp, _DataNode):
                    try:
                        arg_inp = _Constant(arg_inp, device="cpu")
                    except Exception as e:
                        raise TypeError(
                            f"Expected inputs of type "
                            f"`DataNode` or convertible to constant nodes. Received "
                            f"input `{k}` of type '{type(arg_inp).__name__}'.") from e

                _check_arg_input(op._schema, type(self._op).__name__, k)

                self._spec.AddArgumentInput(k, arg_inp.name)
                self._inputs = list(self._inputs) + [arg_inp]

        if self._op.schema.IsDeprecated():
            # TODO(klecki): how to know if this is fn or ops?
            msg = "WARNING: `{}` is now deprecated".format(_op_name(type(self._op).__name__, "fn"))
            use_instead = _op_name(self._op.schema.DeprecatedInFavorOf(), "fn")
            if use_instead:
                msg += ". Use `" + use_instead + "` instead."
            explanation = self._op.schema.DeprecationMessage()
            if explanation:
                msg += "\n" + explanation
            with warnings.catch_warnings():
                warnings.simplefilter("default")
                warnings.warn(msg, DeprecationWarning, stacklevel=2)

    def check_args(self):
        self._op.schema.CheckArgs(self._spec)

    def generate_outputs(self):
        pipeline = _Pipeline.current()
        if pipeline is None and self._op.preserve:
            _Pipeline._raise_pipeline_required("Operators with side-effects ")
        # Add outputs
        if self._op.device == "gpu" or self._op.device == "mixed":
            output_device = "gpu"
        else:
            output_device = "cpu"

        num_output = (self._op.schema.CalculateOutputs(self._spec)
                      + self._op.schema.CalculateAdditionalOutputs(self._spec))

        if num_output == 0 and self._op.preserve:
            t_name = type(self._op).__name__ + "_id_" + str(self.id) + "_sink"
            pipeline.add_sink(_DataNode(t_name, output_device, self))
            return

        for i in range(num_output):
            t_name = self._name
            if num_output > 1:
                t_name += "[{}]".format(i)
            t = _DataNode(t_name, output_device, self)
            self._spec.AddOutput(t.name, t.device)
            if self._op.preserve:
                pipeline.add_sink(t)
            self.append_output(t)

    @property
    def id(self):
        return self._counter.id

    @property
    def inputs(self):
        return self._inputs

    @property
    def outputs(self):
        return self._outputs

    @property
    def unwrapped_outputs(self):
        if len(self._outputs) == 1:
            return self._outputs[0]
        else:
            return self._outputs

    @property
    def spec(self):
        return self._spec

    @property
    def name(self):
        return self._name

    @property
    def relation_id(self):
        return self._relation_id

    @relation_id.setter
    def relation_id(self, value):
        self._relation_id = value

    def append_output(self, output):
        self._outputs.append(output)


class _DaliOperatorMeta(type):

    @property
    def __doc__(self):
        return _docs._docstring_generator(self)


def _check_arg_input(schema, op_name, name):
    if name == "name":
        return
    if not schema.IsTensorArgument(name):
        expected_type_name = _type_name_convert_to_string(schema.GetArgumentType(name), False)
        raise TypeError(
            f"The argument `{name}` for operator `{op_name}` should not be a `DataNode` but a "
            f"{expected_type_name}")


def python_op_factory(name, schema_name=None):

    class Operator(metaclass=_DaliOperatorMeta):

        def __init__(self, *, device="cpu", **kwargs):
            schema_name = _schema_name(type(self))
            self._spec = _b.OpSpec(schema_name)
            self._schema = _b.GetSchema(schema_name)

            # Get the device argument. We will need this to determine
            # the device that our outputs will be stored on
            self._device = device
            self._spec.AddArg("device", self._device)

            kwargs, self._call_args = _separate_kwargs(kwargs)

            for k in self._call_args.keys():
                _check_arg_input(self._schema, type(self).__name__, k)

            if "preserve" in kwargs.keys():
                self._preserve = kwargs["preserve"]
                # we don't want to set "preserve" arg twice
                del kwargs["preserve"]
            else:
                self._preserve = False
            self._spec.AddArg("preserve", self._preserve)
            self._preserve = self._preserve or self._schema.IsNoPrune()

            # Check for any deprecated arguments that should be replaced or removed
            arg_names = list(kwargs.keys())
            for arg_name in arg_names:
                if not self._schema.IsDeprecatedArg(arg_name):
                    continue
                meta = self._schema.DeprecatedArgMeta(arg_name)
                new_name = meta['renamed_to']
                removed = meta['removed']
                msg = meta['msg']
                if new_name:
                    if new_name in kwargs:
                        raise TypeError(f"Operator {type(self).__name__} got an unexpected"
                                        f"'{arg_name}' deprecated argument when '{new_name}'"
                                        f"was already provided")
                    kwargs[new_name] = kwargs[arg_name]
                    del kwargs[arg_name]
                elif removed:
                    del kwargs[arg_name]

                with warnings.catch_warnings():
                    warnings.simplefilter("default")
                    warnings.warn(msg, DeprecationWarning, stacklevel=2)

            # Store the specified arguments
            _add_spec_args(self._schema, self._spec, kwargs)

        @property
        def spec(self):
            return self._spec

        @property
        def schema(self):
            return self._schema

        @property
        def device(self):
            return self._device

        @property
        def preserve(self):
            return self._preserve

        def __call__(self, *inputs, **kwargs):
            self._check_schema_num_inputs(inputs)

            inputs = _preprocess_inputs(inputs, self.__class__.__name__, self._device, self._schema)

            input_sets = self._build_input_sets(inputs)

            # Create OperatorInstance for every input set
            op_instances = []
            for input_set in input_sets:
                op_instances.append(_OperatorInstance(input_set, self, **kwargs))
                op_instances[-1].generate_outputs()

            # Tie the instances together
            relation_id = op_instances[0].id
            for op in op_instances:
                op.relation_id = relation_id

            # If we don't have multiple input sets, flatten the result
            if len(op_instances) == 1:
                result = op_instances[0].unwrapped_outputs
            else:
                outputs = []
                for op in op_instances:
                    outputs.append(op.outputs)
                result = self._repack_output_sets(outputs)
            if _conditionals.conditionals_enabled():
                if len(op_instances) != 1:
                    raise ValueError("Multiple input sets are not supported with conditional"
                                     " execution (when `enable_conditionals=True`)")
                _conditionals.register_data_nodes(result, input_sets[0], kwargs)
            return result

        # Check if any of inputs is a list
        def _detect_multiple_input_sets(self, inputs):
            return any(isinstance(input, list) for input in inputs)

        # Check if all list representing multiple input sets have the same length and return it
        def _check_common_length(self, inputs):
            arg_list_len = max(self._safe_len(input) for input in inputs)
            for input in inputs:
                if isinstance(input, list):
                    if len(input) != arg_list_len:
                        raise ValueError(f"All argument lists for Multiple Input Sets used "
                                         f"with operator {type(self).__name__} must have "
                                         f"the same length")
            return arg_list_len

        def _safe_len(self, input):
            if isinstance(input, list):
                return len(input)
            else:
                return 1

        # Pack single _DataNodes into lists, so they are treated as Multiple Input Sets
        # consistently with the ones already present
        def _unify_lists(self, inputs, arg_list_len):
            result = ()
            for input in inputs:
                if isinstance(input, list):
                    result = result + (input, )
                else:
                    result = result + ([input] * arg_list_len, )
            return result

        # Zip the list from [[arg0, arg0', arg0''], [arg1', arg1'', arg1''], ...]
        # to [(arg0, arg1, ...), (arg0', arg1', ...), (arg0'', arg1'', ...)]
        def _repack_input_sets(self, inputs):
            return self._repack_list(inputs, tuple)

        # Unzip the list from [[out0, out1, out2], [out0', out1', out2'], ...]
        # to [[out0, out0', ...], [out1, out1', ...], [out2, out2', ...]]
        # Assume that all elements of input have the same length
        # If the inputs were 1-elem lists, return just a list, that is:
        # [[out0], [out0'], [out0''], ...] -> [out0, out0', out0'', ...]
        def _repack_output_sets(self, outputs):
            if len(outputs) > 1 and len(outputs[0]) == 1:
                output = []
                for elem in outputs:
                    output.append(elem[0])
                return output
            return self._repack_list(outputs, list)

        # Repack list from [[a, b, c], [a', b', c'], ....]
        # to [fn(a, a', ...), fn(b, b', ...), fn(c, c', ...)]
        # where fn can be `tuple` or `list`
        # Assume that all elements of input have the same length
        def _repack_list(self, sets, fn):
            output_list = []
            arg_list_len = len(sets[0])
            for i in range(arg_list_len):
                output_list.append(fn(input_set[i] for input_set in sets))
            return output_list

        def _check_schema_num_inputs(self, inputs):
            if len(inputs) < self._schema.MinNumInput() or len(inputs) > self._schema.MaxNumInput():
                raise ValueError(
                    f"Operator {type(self).__name__} expects "
                    f"from {self._schema.MinNumInput()} to {self._schema.MaxNumInput()} inputs, "
                    f"but received {len(inputs)}.")

        def _build_input_sets(self, inputs):
            # Build input sets, most of the time we only have one
            input_sets = []
            if self._detect_multiple_input_sets(inputs):
                arg_list_len = self._check_common_length(inputs)
                packed_inputs = self._unify_lists(inputs, arg_list_len)
                input_sets = self._repack_input_sets(packed_inputs)
            else:
                input_sets = [inputs]

            return input_sets

    Operator.__name__ = str(name)
    Operator.schema_name = schema_name or Operator.__name__
    Operator.__call__.__doc__ = _docs._docstring_generator_call(Operator.schema_name)
    return Operator


def _wrap_op(op_class, submodule=[], parent_module=None):
    return _functional._wrap_op(op_class, submodule, parent_module,
                                _docs._docstring_generator_fn(op_class))


def _load_ops():
    _registry._discover_ops()
    _all_ops = _registry._all_registered_ops()
    ops_module = sys.modules[__name__]

    for op_reg_name in _all_ops:
        # TODO(klecki): Make this a function: _add_op(op_reg_name) and invoke it immediately
        # with register_xxx_op(). Now it relies on those class being present in this module
        # at the time of registration.
        schema = _b.TryGetSchema(op_reg_name)
        make_hidden = schema.IsDocHidden() if schema else False
        _, submodule, op_name = _process_op_name(op_reg_name, make_hidden)
        module = _internal.get_submodule(ops_module, submodule)
        if not hasattr(module, op_name):
            op_class = python_op_factory(op_name, op_reg_name)
            op_class.__module__ = module.__name__
            setattr(module, op_name, op_class)

            if op_name not in ["ExternalSource"]:
                _wrap_op(op_class, submodule)

            # The operator was inserted into nvidia.dali.ops.hidden module, let's import it here
            # so it would be usable, but not documented as coming from other module
            if make_hidden:
                parent_module = _internal.get_submodule(ops_module, submodule[:-1])
                setattr(parent_module, op_name, op_class)


def Reload():
    _load_ops()


class _TFRecordReaderImpl():
    """ custom wrappers around ops """

    def __init__(self, path, index_path, features, **kwargs):
        if isinstance(path, list):
            self._path = path
        else:
            self._path = [path]
        if isinstance(index_path, list):
            self._index_path = index_path
        else:
            self._index_path = [index_path]
        self._schema = _b.GetSchema(self._internal_schema_name)
        self._spec = _b.OpSpec(self._internal_schema_name)
        self._device = "cpu"

        self._spec.AddArg("path", self._path)
        self._spec.AddArg("index_path", self._index_path)

        kwargs, self._call_args = _separate_kwargs(kwargs)

        for key, value in kwargs.items():
            self._spec.AddArg(key, value)

        self._features = features

    @property
    def spec(self):
        return self._spec

    @property
    def schema(self):
        return self._schema

    @property
    def device(self):
        return self._device

    def __call__(self, *inputs, **kwargs):
        # We do not handle multiple input sets for Reader as they do not have inputs
        if (len(inputs) > self._schema.MaxNumInput() or len(inputs) < self._schema.MinNumInput()):
            raise ValueError(
                f"Operator {type(self).__name__} expects "
                f"from {self._schema.MinNumInput()} to {self._schema.MaxNumInput()} inputs, "
                f"but received {len(inputs)}.")

        op_instance = _OperatorInstance(inputs, self, **kwargs)
        outputs = {}
        feature_names = []
        features = []
        for i, (feature_name, feature) in enumerate(self._features.items()):
            t_name = op_instance._name
            if len(self._features.items()) > 1:
                t_name += "[{}]".format(i)

            t = _DataNode(t_name, self._device, op_instance)
            op_instance.spec.AddOutput(t.name, t.device)
            op_instance.append_output(t)
            outputs[feature_name] = t
            feature_names.append(feature_name)
            features.append(feature)

        # We know this reader doesn't have any inputs
        if _conditionals.conditionals_enabled():
            _conditionals.register_data_nodes(list(outputs.values()))

        op_instance.spec.AddArg("feature_names", feature_names)
        op_instance.spec.AddArg("features", features)
        return outputs


def _load_readers_tfrecord():
    _TFRecordReaderImpl.__call__.__doc__ = _docs._docstring_generator_call("readers__TFRecord")

    _registry.register_cpu_op('readers__TFRecord')
    _registry.register_cpu_op('TFRecordReader')

    ops_module = sys.modules[__name__]

    class TFRecordReader(_TFRecordReaderImpl, metaclass=_DaliOperatorMeta):
        pass

    class TFRecord(_TFRecordReaderImpl, metaclass=_DaliOperatorMeta):
        pass

    for op_reg_name, internal_schema, op_class in [
        ('readers__TFRecord', 'readers___TFRecord', TFRecord),
        ('TFRecordReader', '_TFRecordReader', TFRecordReader)
    ]:
        op_class.schema_name = op_reg_name
        op_class._internal_schema_name = internal_schema
        op_full_name, submodule, op_name = _process_op_name(op_reg_name)
        module = _internal.get_submodule(ops_module, submodule)
        if not hasattr(module, op_name):
            op_class.__module__ = module.__name__
            setattr(module, op_name, op_class)
            _wrap_op(op_class, submodule)


class PythonFunctionBase(metaclass=_DaliOperatorMeta):

    def __init__(self, impl_name, function, num_outputs=1, device='cpu', **kwargs):
        self._schema = _b.GetSchema(impl_name)
        self._spec = _b.OpSpec(impl_name)
        self._device = device
        self._impl_name = impl_name

        kwargs, self._call_args = _separate_kwargs(kwargs)

        for key, value in kwargs.items():
            self._spec.AddArg(key, value)

        self.function = function
        self.num_outputs = num_outputs
        self._preserve = True

    @property
    def spec(self):
        return self._spec

    @property
    def schema(self):
        return self._schema

    @property
    def device(self):
        return self._device

    @property
    def preserve(self):
        return self._preserve

    def __call__(self, *inputs, **kwargs):
        inputs = _preprocess_inputs(inputs, self._impl_name, self._device, None)
        pipeline = _Pipeline.current()
        if pipeline is None:
            _Pipeline._raise_pipeline_required("PythonFunction operator")

        if (len(inputs) > self._schema.MaxNumInput() or len(inputs) < self._schema.MinNumInput()):
            raise ValueError(
                f"Operator {type(self).__name__} expects "
                f"from {self._schema.MinNumInput()} to {self._schema.MaxNumInput()} inputs, "
                f"but received {len(inputs)}.")
        for inp in inputs:
            if not isinstance(inp, _DataNode):
                raise TypeError(f"Expected inputs of type `DataNode`. "
                                f"Received input of type '{type(inp).__name__}'. "
                                f"Python Operators do not support Multiple Input Sets.")
        op_instance = _OperatorInstance(inputs, self, **kwargs)
        op_instance.spec.AddArg("function_id", id(self.function))
        op_instance.spec.AddArg("num_outputs", self.num_outputs)
        op_instance.spec.AddArg("device", self.device)
        if self.num_outputs == 0:
            t_name = self._impl_name + "_id_" + str(op_instance.id) + "_sink"
            t = _DataNode(t_name, self._device, op_instance)
            pipeline.add_sink(t)
            return
        outputs = []

        for i in range(self.num_outputs):
            t_name = op_instance._name
            if self.num_outputs > 1:
                t_name += "[{}]".format(i)
            t = _DataNode(t_name, self._device, op_instance)
            op_instance.spec.AddOutput(t.name, t.device)
            op_instance.append_output(t)
            pipeline.add_sink(t)
            outputs.append(t)

        if _conditionals.conditionals_enabled():
            _conditionals.register_data_nodes(outputs, inputs, kwargs)
        return outputs[0] if len(outputs) == 1 else outputs


def _dlpack_to_array(dlpack):
    return nvidia.dali.python_function_plugin.DLTensorToArray(dlpack)


def _dlpack_from_array(array):
    return nvidia.dali.python_function_plugin.ArrayToDLTensor(array)


class PythonFunction(PythonFunctionBase):
    schema_name = "PythonFunction"
    _registry.register_cpu_op('PythonFunction')
    _registry.register_gpu_op('PythonFunction')

    @staticmethod
    def current_stream():
        """Gets DALI's current CUDA stream."""
        return _CUDAStream(nvidia.dali.python_function_plugin.current_dali_stream())

    @staticmethod
    def check_outputs(outputs, num_outputs):
        if num_outputs > 1:
            if not isinstance(outputs, tuple):
                raise TypeError(
                    "The output from a multi-output Python"
                    "function operator must be a tuple, got: ", type(outputs))
            if len(outputs) != num_outputs:
                raise ValueError(f"Unexpected number of outputs from Python"
                                 f"function operator - got {len(outputs)}, expected {num_outputs}")

    @staticmethod
    def function_wrapper_per_sample(function, num_outputs, from_dlpack, to_dlpack, *dlpack_inputs):
        arrays = [from_dlpack(dlpack) for dlpack in dlpack_inputs]
        arr_out = function(*arrays)
        if arr_out is None:
            return
        PythonFunction.check_outputs(arr_out, num_outputs)
        if isinstance(arr_out, tuple):
            return tuple(map(lambda t: to_dlpack(t), arr_out))
        else:
            return to_dlpack(arr_out)

    @staticmethod
    def function_wrapper_batch(function, num_outputs, from_dlpack, to_dlpack, *dlpack_inputs):
        arrays = [[from_dlpack(dlpack) for dlpack in dl_input] for dl_input in dlpack_inputs]
        arr_outs = function(*arrays)
        if arr_outs is None:
            return

        def convert_batch(batch):
            if isinstance(batch, list):
                return [to_dlpack(x) for x in batch]
            else:
                return to_dlpack(batch)

        PythonFunction.check_outputs(arr_outs, num_outputs)
        if isinstance(arr_outs, tuple):
            return tuple(convert_batch(x) for x in arr_outs)
        else:
            return convert_batch(arr_outs)

    @staticmethod
    def _function_wrapper_cpu(batch_processing, function, num_outputs, *dlpack_inputs):
        if batch_processing:
            return PythonFunction.function_wrapper_batch(
                function,
                num_outputs,
                _dlpack_to_array,
                _dlpack_from_array,
                *dlpack_inputs)
        else:
            return PythonFunction.function_wrapper_per_sample(
                function,
                num_outputs,
                _dlpack_to_array,
                _dlpack_from_array,
                *dlpack_inputs)

    @staticmethod
    def _cupy_stream_wrapper(function, *inputs):
        stream = cupy.cuda.Stream(null=True)
        stream.ptr = PythonFunction.current_stream().ptr
        with stream:
            out = function(*inputs)
        stream.ptr = 0
        return out

    @staticmethod
    def _function_wrapper_gpu(batch_processing, function, num_outputs, *dlpack_inputs):

        def wrapped_func(*inputs):
            return PythonFunction._cupy_stream_wrapper(function, *inputs)

        if batch_processing:
            return PythonFunction.function_wrapper_batch(wrapped_func, num_outputs, cupy.fromDlpack,
                                                         lambda t: t.toDlpack(), *dlpack_inputs)
        else:
            return PythonFunction.function_wrapper_per_sample(wrapped_func, num_outputs,
                                                              cupy.fromDlpack,
                                                              lambda t: t.toDlpack(),
                                                              *dlpack_inputs)

    def __init__(self, function, num_outputs=1, device='cpu', batch_processing=False, **kwargs):
        if device == 'gpu':
            _setup_cupy()

        if device == 'cpu':
            def func(*ts):
                return PythonFunction._function_wrapper_cpu(
                    batch_processing, function, num_outputs, *ts)
        else:
            def func(*ts):
                return PythonFunction._function_wrapper_gpu(
                    batch_processing, function, num_outputs, *ts)

        super(PythonFunction,
              self).__init__(
                impl_name="DLTensorPythonFunctionImpl",
                function=func,
                num_outputs=num_outputs,
                device=device,
                synchronize_stream=False,
                batch_processing=batch_processing,
                **kwargs)


class DLTensorPythonFunction(PythonFunctionBase):
    schema_name = "DLTensorPythonFunction"
    _registry.register_cpu_op('DLTensorPythonFunction')
    _registry.register_gpu_op('DLTensorPythonFunction')

    @staticmethod
    def _function_wrapper_dlpack(batch_processing, function, num_outputs, *dlpack_inputs):
        if batch_processing:
            return PythonFunction.function_wrapper_batch(function,
                                                         num_outputs,
                                                         lambda x: x,
                                                         lambda x: x,
                                                         *dlpack_inputs)
        else:
            return PythonFunction.function_wrapper_per_sample(function,
                                                              num_outputs,
                                                              lambda x: x,
                                                              lambda x: x,
                                                              *dlpack_inputs)

    def __init__(self, function, num_outputs=1, device='cpu', synchronize_stream=True,
                 batch_processing=True, **kwargs):

        def func(*ts):
            return DLTensorPythonFunction._function_wrapper_dlpack(
                batch_processing, function, num_outputs, *ts)

        super(DLTensorPythonFunction,
              self).__init__(impl_name="DLTensorPythonFunctionImpl",
                             function=func,
                             num_outputs=num_outputs,
                             device=device,
                             synchronize_stream=synchronize_stream,
                             batch_processing=batch_processing,
                             **kwargs)


_wrap_op(PythonFunction)
_wrap_op(DLTensorPythonFunction)


def _choose_device(inputs):
    for input in inputs:
        if isinstance(input, (tuple, list)):
            if any(getattr(inp, "device", None) == "gpu" for inp in input):
                return "gpu"
        elif getattr(input, "device", None) == "gpu":
            return "gpu"
    return "cpu"


def _preprocess_inputs(inputs, op_name, device, schema=None):
    if isinstance(inputs, tuple):
        inputs = list(inputs)

    def is_input(x):
        if isinstance(x, (_DataNode, nvidia.dali.types.ScalarConstant)):
            return True
        return (isinstance(x, (list))
                and any(isinstance(y, _DataNode) for y in x)
                and all(isinstance(y, (_DataNode, nvidia.dali.types.ScalarConstant)) for y in x))

    default_input_device = "gpu" if device == "gpu" else "cpu"

    for idx, inp in enumerate(inputs):
        if not is_input(inp):
            if schema:
                input_device = schema.GetInputDevice(idx) or default_input_device
            else:
                input_device = default_input_device
            if not isinstance(inp, nvidia.dali.types.ScalarConstant):
                try:
                    inp = _Constant(inp, device=input_device)
                except Exception as ex:
                    raise TypeError(f"""when calling operator {op_name}:
Input {idx} is neither a DALI `DataNode` nor a list of data nodes but `{type(inp).__name__}`.
Attempt to convert it to a constant node failed.""") from ex

            if not isinstance(inp, _DataNode):
                inp = nvidia.dali.ops._instantiate_constant_node(input_device, inp)

        inputs[idx] = inp
    return inputs


def _is_boolean_like(input):
    if type(input) is bool:
        return True
    if isinstance(input, _ScalarConstant):
        if input.dtype in _bool_types:
            return True
    return False


# Boolean and integer types are considered integer-like


def _is_integer_like(input):
    if _is_boolean_like(input):
        return True
    if type(input) is int:
        return True
    if isinstance(input, _ScalarConstant):
        if input.dtype in _int_like_types:
            return True
    return False


def _is_real_like(input):
    if type(input) is float:
        return True
    if isinstance(input, _ScalarConstant):
        if input.dtype in _float_types:
            return True
    return False


def _to_type_desc(input):
    """ <type> description required by ArithmeticGenericOp """
    if type(input) is bool:
        return "bool"
    if type(input) is int:
        return "int32"
    if type(input) is float:
        return "float32"  # TODO(klecki): current DALI limitation
    if isinstance(input, _ScalarConstant):
        dtype_to_desc = {
            DALIDataType.BOOL:    "bool",
            DALIDataType.INT8:    "int8",
            DALIDataType.INT16:   "int16",
            DALIDataType.INT32:   "int32",
            DALIDataType.INT64:   "int64",
            DALIDataType.UINT8:   "uint8",
            DALIDataType.UINT16:  "uint16",
            DALIDataType.UINT32:  "uint32",
            DALIDataType.UINT64:  "uint64",
            DALIDataType.FLOAT16: "float16",
            DALIDataType.FLOAT:   "float32",
            DALIDataType.FLOAT64: "float64",
        }
        return dtype_to_desc[input.dtype]

    raise TypeError(
        f"Constant argument to arithmetic operation not supported. "
        f"Got {str(type(input))}, expected "
        f"a constant value of type 'bool', 'int', 'float' or 'nvidia.dali.types.Constant'.")


# Group inputs into categories_idxs, edges of type ``edge_type``,
# integer constants and real constants.
# The categories_idxs is a list that for an input `i` contains a tuple:
# (category of ith input, index of ith input in appropriate category)
def _group_inputs(inputs, edge_type=_DataNode):
    categories_idxs = []
    edges = []
    integers = []
    reals = []
    for input in inputs:
        if not isinstance(input, (edge_type, _ScalarConstant, int, float)):
            input = nvidia.dali.types.Constant(input)
        if isinstance(input, edge_type):
            categories_idxs.append(("edge", len(edges)))
            edges.append(input)
        elif _is_integer_like(input):
            categories_idxs.append(("integer", len(integers)))
            integers.append(input)
        elif _is_real_like(input):
            categories_idxs.append(("real", len(reals)))
            reals.append(input)
        else:
            raise TypeError(
                f"Argument to arithmetic operation not supported."
                f"Got {str(type(input))}, expected a return value from other"
                f"DALI Operator  or a constant value of type 'bool', 'int', "
                f"'float' or 'nvidia.dali.types.Constant'.")

    if len(integers) == 0:
        integers = None
    if len(reals) == 0:
        reals = None
    return (categories_idxs, edges, integers, reals)


def _generate_input_desc(categories_idx, integers, reals):
    """
    Generate the list of <input> subexpression as specified
    by grammar for ArithmeticGenericOp
    """
    input_desc = ""
    for i, (category, idx) in enumerate(categories_idx):
        if category == "edge":
            input_desc += "&{}".format(idx)
        elif category == "integer":
            input_desc += "${}:{}".format(idx, _to_type_desc(integers[idx]))
        elif category == "real":
            input_desc += "${}:{}".format(idx, _to_type_desc(reals[idx]))
        if i < len(categories_idx) - 1:
            input_desc += " "
    return input_desc


def _arithm_op(name, *inputs):
    """
    Create arguments for ArithmeticGenericOp and call it with supplied inputs.
    Select the `gpu` device if at least one of the inputs is `gpu`, otherwise `cpu`.
    """
    categories_idxs, edges, integers, reals = _group_inputs(inputs)
    input_desc = _generate_input_desc(categories_idxs, integers, reals)
    expression_desc = "{}({})".format(name, input_desc)
    dev = _choose_device(edges)
    # Create "instance" of operator
    op = ArithmeticGenericOp(       # noqa: F821
        device=dev,
        expression_desc=expression_desc,
        integer_constants=integers,
        real_constants=reals)
    # If we are on gpu, we must mark all inputs as gpu
    if dev == "gpu":
        dev_inputs = list(edge.gpu() for edge in edges)
    else:
        dev_inputs = edges

    # Call it immediately
    result = op(*dev_inputs)
    if _conditionals.conditionals_enabled():
        _conditionals.register_data_nodes(result, dev_inputs)
    return result


# This must go at the end - the purpose of these imports is to expose the operators in
# nvidia.dali.ops module
from nvidia.dali.external_source import ExternalSource  # noqa: E402

ExternalSource.__module__ = __name__


class _CompoundOp:

    def __init__(self, op_list):
        self._ops = []
        for op in op_list:
            if isinstance(op, _CompoundOp):
                self._ops += op._ops
            else:
                self._ops.append(op)

    def __call__(self, *inputs, **kwargs):
        inputs = list(inputs)
        for op in self._ops:
            for i in range(len(inputs)):
                if inputs[i].device == "cpu" and op.device == "gpu" and op.schema.GetInputDevice(
                        i) != "cpu":
                    inputs[i] = inputs[i].gpu()
            inputs = op(*inputs, **kwargs)
            kwargs = {}
            if isinstance(inputs, tuple):
                inputs = list(inputs)
            if isinstance(inputs, _DataNode):
                inputs = [inputs]

        return inputs[0] if len(inputs) == 1 else inputs


def Compose(op_list):
    """Returns a meta-operator that chains the operations in op_list.

The return value is a callable object which, when called, performs::

    op_list[n-1](op_list([n-2](...  op_list[0](args))))

Operators can be composed only when all outputs of the previous operator can be processed directly
by the next operator in the list.

The example below chains an image decoder and a Resize operation with random square size.
The  ``decode_and_resize`` object can be called as if it was an operator::

    decode_and_resize = ops.Compose([
        ops.decoders.Image(device="cpu"),
        ops.Resize(size=fn.random.uniform(range=400,500)), device="gpu")
    ])

    files, labels = fn.readers.caffe(path=caffe_db_folder, seed=1)
    pipe.set_ouputs(decode_and_resize(files), labels)

If there's a transition from CPU to GPU in the middle of the ``op_list``, as is the case in this
example, ``Compose`` automatically arranges copying the data to GPU memory.


.. note::
    This is an experimental feature, subject to change without notice.
"""
    return op_list[0] if len(op_list) == 1 else _CompoundOp(op_list)


_registry.register_cpu_op('Compose')
_registry.register_gpu_op('Compose')


_load_ops()

try:
    _load_readers_tfrecord()
except RuntimeError:
    # TFRecord can be disabled (custom build). No need to fail
    pass