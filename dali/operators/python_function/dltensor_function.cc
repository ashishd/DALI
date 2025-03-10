// Copyright (c) 2019-2024, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include <pybind11/stl.h>
#include <memory>
#include <utility>
#include <string>
#include <optional>
#include "dali/operators/python_function/dltensor_function.h"
#include "dali/pipeline/data/dltensor_obj.h"
#include "dali/pipeline/util/copy_with_stride.h"

namespace dali {

DALI_SCHEMA(DLTensorPythonFunctionImpl)
    .AddOptionalArg("synchronize_stream", "Synchronize CUDA stream", true)
    .AddArg("function_id", R"code(Id of the python function)code", DALI_INT64)
    .AddOptionalArg("num_outputs", R"code(Number of outputs)code", 1)
    .AddArg("batch_processing", "Batch processing.", DALI_BOOL)
    .NumInput(0, 256)
    .OutputFn([](const OpSpec &spec) {return spec.GetArgument<int>("num_outputs");})
    .AddOptionalArg<std::vector<TensorLayout>>("output_layouts",
        R"code(Tensor data layouts for the outputs.

This argument can be a list that contains a distinct layout for each output. If the list has
fewer than num_outputs elements, only the first outputs have the layout set and the rest of the
outputs have no layout assigned.)code", nullptr)
    .NoPrune()
    .Unserializable()
    .MakeInternal();

DALI_SCHEMA(DLTensorPythonFunction)
    .DocStr(R"code(Executes a Python function that operates on DLPack tensors.

The function should not modify input tensors.

For the GPU operator, it is the user's responsibility to synchronize the device code with DALI.
To synchronize the device code with DALI, synchronize DALI's work before the operator call
with the ``synchronize_stream`` flag (enabled by default) and ensure that the scheduled device
tasks are finished in the operator call. The GPU code can be executed on the CUDA stream used
by DALI, which can be obtained by calling the ``current_dali_stream()`` function. In this case,
the ``synchronize_stream`` flag can be set to False.

.. warning::
  This operator is not compatible with TensorFlow integration.
)code")
    .AddOptionalArg("synchronize_stream",
        R"code(Ensures that DALI synchronizes its CUDA stream before calling the Python function.

.. warning::
  This argument should be set to False only if the called function schedules device
  work to the stream that is used by DALI.)code", true)
    .AddOptionalArg("batch_processing",
                    R"code(Determines whether the function is invoked once per batch or
separately for every sample in the batch.

If set to True, the function will receive its arguments as lists of DLPack tensors.)code", false)
    .NumInput(0, 256)
    .AllowSequences()
    .SupportVolumetric()
    .NoPrune()
    .AddParent("PythonFunctionBase");

namespace detail {

template <>
py::list PrepareDLTensorInputs<CPUBackend>(Workspace &ws) {
  py::list input_tuple;
  for (Index idx = 0; idx < ws.NumInput(); ++idx) {
    py::list dl_tensor_list;
    auto &input = ws.UnsafeMutableInput<CPUBackend>(idx);
    for (Index i = 0; i < ws.GetInputBatchSize(idx); ++i) {
      auto dl_capsule = TensorToDLPackView(input[i], input.is_pinned(), input.device_id());
      dl_tensor_list.append(dl_capsule);
    }
    input_tuple.append(dl_tensor_list);
  }
  return input_tuple;
}

template <>
py::list PrepareDLTensorInputs<GPUBackend>(Workspace &ws) {
  py::list input_tuple;
  for (Index idx = 0; idx < ws.NumInput(); ++idx) {
    auto &input = ws.UnsafeMutableInput<GPUBackend>(idx);
    py::list dl_tensor_list = TensorListToDLPackView(input);
    input_tuple.append(dl_tensor_list);
  }
  return input_tuple;
}

template <>
py::list PrepareDLTensorInputsPerSample<CPUBackend>(Workspace &ws) {
  py::list input_tuples;
  if (ws.NumInput() == 0) return input_tuples;
  auto batch_size = ws.GetInputBatchSize(0);
  for (Index s = 0; s < batch_size; ++s) {
    py::list tuple;
    for (Index idx = 0; idx < ws.NumInput(); ++idx) {
      auto &input = ws.UnsafeMutableInput<CPUBackend>(idx);
      auto dl_capsule = TensorToDLPackView(input[s], input.is_pinned(), input.device_id());
      tuple.append(dl_capsule);
    }
    input_tuples.append(tuple);
  }
  return input_tuples;
}

template <>
py::list PrepareDLTensorInputsPerSample<GPUBackend>(Workspace &ws) {
  std::vector<py::list> input_tuples;
  if (ws.NumInput() == 0) return py::cast(input_tuples);
  Index batch_size = ws.Input<GPUBackend>(0).num_samples();
  input_tuples.resize(batch_size);
  for (Index idx = 0; idx < ws.NumInput(); ++idx) {
    py::list dl_tensor_list = TensorListToDLPackView(ws.UnsafeMutableInput<GPUBackend>(idx));
    for (Index s = 0; s < batch_size; ++s) {
      input_tuples[s].append(dl_tensor_list[s]);
    }
  }
  return py::cast(input_tuples);
}

TensorListShape<> GetDLTensorListShape(const std::vector<DLMTensorPtr>& dl_tensors) {
  TensorListShape<> list_shape{};
  list_shape.resize(dl_tensors.size(), dl_tensors[0]->dl_tensor.ndim);
  for (size_t i = 0; i < dl_tensors.size(); ++i) {
    auto &dl_tensor = dl_tensors[i]->dl_tensor;
    assert(dl_tensor.ndim == list_shape.sample_dim());
    list_shape.set_tensor_shape(i, make_span(dl_tensor.shape, dl_tensor.ndim));
  }
  return list_shape;
}

template <>
void CopyOutputData(TensorList<CPUBackend> &output, std::vector<DLMTensorPtr> &dl_tensors,
                    Workspace &workspace) {
  int batch_size = dl_tensors.size();
  auto &thread_pool = workspace.GetThreadPool();
  auto out_shape = output.shape();
  for (int i = 0; i < batch_size; ++i) {
    thread_pool.AddWork([&, i](int) {
      CopyDlTensorCpu(output.raw_mutable_tensor(i), dl_tensors[i]);
    }, out_shape.tensor_size(i));
  }
  thread_pool.RunAll();
}

template <>
void CopyOutputData(TensorList<GPUBackend>& output, std::vector<DLMTensorPtr> &dl_tensors,
                    Workspace &workspace) {
  CopyDlTensorBatchGpu(output, dl_tensors, workspace.stream());
}

}  // namespace detail

DALI_REGISTER_OPERATOR(DLTensorPythonFunctionImpl, DLTensorPythonFunctionImpl<CPUBackend>, CPU);

DALI_REGISTER_OPERATOR(DLTensorPythonFunctionImpl, DLTensorPythonFunctionImpl<GPUBackend>, GPU);

std::mutex operator_lock;

static cudaStream_t current_cuda_stream = nullptr;

cudaStream_t GetCurrentStream() {
  return current_cuda_stream;
}

void SetCurrentStream(cudaStream_t stream) {
  current_cuda_stream = stream;
}

struct PyBindInitializer {
  PyBindInitializer() {
    auto thread_state = PyGILState_Ensure();
    pybind11::get_shared_data("");  // setup the pybind's internals pointer
    PyGILState_Release(thread_state);
  }
};

// Some pybind's internals are not properly initialized when used in dynamically linked library,
// so this workaround initializes them manually
static PyBindInitializer pybind_initializer{}; // NOLINT

struct PyArrayPayload : TensorViewPayload {
  explicit PyArrayPayload(const py::array &array) : array(array) {
    shape = TensorShape<>(array.shape(), array.shape() + array.ndim());
    strides.resize(shape.size());
    auto itemsize = array.dtype().itemsize();
    for (int i = 0; i < array.ndim(); ++i) {
      strides[i] = array.strides(i) / itemsize;
    }
  }
  py::array array;
};


using DLTensorNumpyResource = DLTensorResource<PyArrayPayload>;

auto GetDLTensorResource(const py::array &array) {
  auto rsrc = DLTensorNumpyResource::Create(array);
  auto &tensor = rsrc->dlm_tensor.dl_tensor;
  auto buffer = rsrc->payload.array.request();
  tensor.data = buffer.ptr;
  tensor.shape = rsrc->payload.shape.data();
  tensor.ndim = rsrc->payload.shape.size();
  tensor.strides = rsrc->payload.strides.empty() ? nullptr : rsrc->payload.strides.data();
  tensor.device = ToDLDevice(false, false, 0);
  tensor.dtype = ToDLType(TypeFromFormatStr(buffer.format).id());
  return rsrc;
}


PYBIND11_MODULE(python_function_plugin, m) {
  m.def("current_dali_stream", []() { return reinterpret_cast<uint64_t>(GetCurrentStream()); });

  m.def("DLTensorToArray", [](py::capsule dl_capsule) {
    auto dlm_tensor_ptr = DLMTensorPtrFromCapsule(dl_capsule);
    const auto &dl_tensor = dlm_tensor_ptr->dl_tensor;
    auto dali_type = ToDALIType(dl_tensor.dtype);
    py::dtype dtype(FormatStrFromType(dali_type));
    auto shape = make_span(dl_tensor.shape, dl_tensor.ndim);
    py::array array;
    if (dl_tensor.strides) {
      TensorShape<> strides;
      strides.resize(dl_tensor.ndim);
      for (int i = 0; i < dl_tensor.ndim; ++i) {
        strides[i] = dl_tensor.strides[i] * dtype.itemsize();
      }
      array = py::array(dtype, shape, strides, dl_tensor.data, py::array());
    } else {
      array = py::array(dtype, shape, dl_tensor.data, py::array());
    }
    return array;
  });

  m.def("ArrayToDLTensor", [](py::array array) {
    auto rsrc = GetDLTensorResource(array);
    return DLTensorToCapsule(ToDLMTensor(std::move(rsrc)));
  });

  // For the _a suffix
  using namespace pybind11::literals;  // NOLINT

  py::class_<DLTensorObj>(m, "DLTensorObj")
      .def("__dlpack_device__", &DLTensorObj::dlpack_device)
      .def(
          "__dlpack__",
          [](DLTensorObj &self, std::optional<int64_t> stream) {
            std::optional<cudaStream_t> cuda_stream{};
            if (stream.has_value()) {
              cuda_stream = reinterpret_cast<cudaStream_t>(*stream);
            }
            DLManagedTensor *data_ptr = self.dlpack(cuda_stream);
            return py::capsule(data_ptr, DLTENSOR_NAME, &DLTensorCapsuleDestructor);
          },
          "stream"_a);
}

}  // namespace dali
