# coding=utf-8
# Copyright 2022 Google LLC.
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

"""Python utility functions for JAX which contains minimal TF lingvo deps."""

import contextlib
import dataclasses
import functools
import re
from typing import Any, Callable, Iterable, Optional, Sequence, Union

from absl import flags
from absl import logging
import flax
import jax
from jax.experimental import global_device_array as gda_lib
from jax.experimental import maps
from jax.experimental import mesh_utils
from jax.experimental import multihost_utils
from jax.experimental import pjit
from jax.interpreters import pxla
import jax.numpy as jnp
from lingvo.core import cluster
from lingvo.core import hyperparams
from lingvo.core import py_utils
import numpy as np
import optax

flags.DEFINE_bool(
    'pmap_use_tensorstore', False,
    'Temporary flag to allow pmap users to fall back to flax checkpointing.')


def pmap_use_tensorstore():
  return flags.FLAGS.pmap_use_tensorstore


infeed_context_scope = cluster.InfeedContextScope
# No more symbols from lingvo cluster should be accessed by JAX library.

flatten = py_utils.Flatten
NestedMap = py_utils.NestedMap
MergeDictsWithValueCheck = py_utils.MergeDictsWithValueCheck
ThreadLocalDict = py_utils.ThreadLocalDict
ThreadLocalStack = py_utils.ThreadLocalStack
fprop_dtype = py_utils.FPropDtype
sharded_file_pattern_to_glob = py_utils.ShardedFilePatternToGlob
# No more symbols from lingvo py_utils should be accessed by JAX library.
del py_utils

InstantiableParams = hyperparams.InstantiableParams
# Rename to HParams.
HParams = hyperparams.Params
# No more symbols from lingvo hyperparams should be accessed by JAX library.
del hyperparams

# No more imports from lingvo should be accessed by core JAX library.
JTensor = jnp.ndarray


# A utility function to flatten copied from jax/_src/util.py
def _unzip2(xys):
  xs = []
  ys = []
  for x, y in xys:
    xs.append(x)
    ys.append(y)
  return tuple(xs), tuple(ys)


jax.tree_util.register_pytree_node(NestedMap,
                                   lambda xs: _unzip2(sorted(xs.items()))[::-1],
                                   lambda keys, xs: NestedMap(zip(keys, xs)))


@functools.partial(functools.partial, jax.tree_map)
def assert_same_shape_and_dtype(x, y):
  assert x.shape == y.shape and x.dtype == y.dtype, f'x={x}, y={y}'


def reshard(array: jnp.ndarray) -> np.ndarray:
  """Reshards an input tensor according to the number of local devices."""
  num_devices = jax.local_device_count()
  batch_size = array.shape[0]
  return np.reshape(array,
                    (num_devices, batch_size // num_devices) + array.shape[1:])


def unshard(array: jnp.ndarray) -> np.ndarray:
  """Undo the resharding to reshape away the local device count leading dim."""
  return np.reshape(array, (-1,) + array.shape[2:])


def _unreplicate(x):
  """Helper to unreplicated the data based on its type."""
  if isinstance(x, gda_lib.GlobalDeviceArray):
    return x.local_data(0)
  elif isinstance(x, pxla.ShardedDeviceArray):
    val = x.device_buffers[0]
    # DeviceArrays returned by the `.device_buffers` property of SDA might not
    # have avals set on them. So set the avals before returning so that
    # computations don't error down the stack.
    if val.aval is None:
      val.aval = jax.ShapedArray(val.shape, val.dtype)
    return val
  else:
    return x


def maybe_unreplicate_for_fully_replicated(data):
  """Fully replicated data.

  Data may contain multiple shards, but here we assume data is fully replicated,
  and we unreplicate 'data' by taking just the first shard. In
  the following cases, 'data' are fully replicated:
  1. All metrics are fully replicated. In pmap training, we explicitly
     synchronize metrics across different data replicas, and as a result
     metrics are fully replicated (identical across different replicas).
     In spmd training, there is one single model replica. metric output are
     specifically marked as replicated.
  2. Similarly most summaries are replicated.
  3. In pmap training, model weights are fully replicated.

  Args:
    data: An array containing data.

  Returns:
    First shard of data.
  """
  return jax.tree_map(_unreplicate, data)


def maybe_unreplicate_for_first_shard(data):
  """Unreplicate data for first shard.

  'data' may not be fully replicated.

  `data` may contain multiple shards (as device buffers in multiple devices),
  but here we simply return the first shard.

  Args:
    data: An array containing data.

  Returns:
    First shard of data.
  """
  return jax.tree_map(_unreplicate, data)


def extract_keys(n, p, key_separator, left_separator, right_separator, is_leaf):
  """Alias long function call with fixed separators."""
  return extract_prefixed_keys_from_nested_map(
      n,
      p,
      key_separator=key_separator,
      left_separator=left_separator,
      right_separator=right_separator,
      is_leaf=is_leaf)


def _handle_dict(
    node,
    prefix,
    key_separator,
    left_separator,
    right_separator,
    node_type=None,
    is_leaf=None,
):
  """Handles dictionaries."""
  result = {}
  for key, value in node.items():
    if prefix:
      path = f'{prefix}{key_separator}{key}'
    else:
      path = key
    result[key] = extract_keys(
        value,
        path,
        key_separator,
        left_separator,
        right_separator,
        is_leaf=is_leaf)
  if node_type is not None:
    return node_type(**result)
  else:
    return type(node)(result)


def extract_prefixed_keys_from_nested_map(
    node: Any,
    prefix: str = '',
    key_separator: str = '/',
    left_separator: str = '[',
    right_separator: str = ']',
    is_leaf: Optional[Callable[[Any], bool]] = None) -> Any:
  """Extracts a NestedMap with the nested prefix keys from its NestedMap node."""
  if is_leaf is not None and is_leaf(node):
    return None
  elif isinstance(node, dict):  # NestedMap inherits from dict.
    return _handle_dict(
        node,
        prefix,
        key_separator,
        left_separator,
        right_separator,
        is_leaf=is_leaf)
  # PartitionSpec is subclass of tuple.
  elif isinstance(node, pjit.PartitionSpec):
    return prefix
  elif isinstance(node, (list, tuple)):
    # Check if it is a NamedTuple.
    if hasattr(node, '_fields'):
      if prefix:
        prefix += f'{key_separator}'
      out = {}
      for field in node._fields:
        out[field] = extract_keys(
            getattr(node, field),
            f'{prefix}{field}',
            key_separator,
            left_separator,
            right_separator,
            is_leaf=is_leaf)
      return type(node)(**out)
    # Convert back to list or tuple.
    out = []
    for i, v in enumerate(node):
      out.append(
          extract_keys(
              v,
              f'{prefix}{left_separator}{i}{right_separator}',
              key_separator,
              left_separator,
              right_separator,
              is_leaf=is_leaf))
    return type(node)(out)
  elif (dataclasses.is_dataclass(node) and
        node.__class__ in flax.serialization._STATE_DICT_REGISTRY):  # pylint: disable=protected-access
    if hasattr(node, '__dict__'):
      node_dict = node.__dict__
    else:
      node_dict = flax.serialization.to_state_dict(node)
    return _handle_dict(
        node_dict,
        prefix,
        key_separator,
        left_separator,
        right_separator,
        node_type=type(node),
        is_leaf=is_leaf,
    )
  if not prefix:
    return None
  return prefix


def sync_global_devices(name: str) -> None:
  """Sync across all hosts/devices."""
  global_device_count = jax.device_count()
  logging.info('Starting sync_global_devices %s across %s devices globally',
               name, global_device_count)
  multihost_utils.sync_global_devices(name)
  logging.info('Finished sync_global_devices %s across %s devices globally',
               name, global_device_count)


def create_gda(host_arrays: np.ndarray, global_shapes: jax.ShapeDtypeStruct,
               global_mesh: maps.Mesh,
               pspecs: Any) -> gda_lib.GlobalDeviceArray:
  """Create GDA from host array.

  Evenly partitioning x along axis 0 and device_put shards to local devices.

  Args:
    host_arrays: host-local arrays.
    global_shapes: global shapes of the resultant GDA.
    global_mesh: global mesh of the resultant GDA.
    pspecs: partition specs of the resultant GDA.

  Returns:
    A GDA with x as the host-local data.
  """

  local_devices = global_mesh.local_devices
  local_device_count = jax.local_device_count()

  def _put_to_devices(x):
    try:
      per_device_arrays = np.split(x, local_device_count, axis=0)
    except ValueError as array_split_error:
      raise ValueError(
          f'Unable to put to devices shape {x.shape} with '
          f'local device count {local_device_count}') from array_split_error
    device_buffers = [
        jax.device_put(arr, d)
        for arr, d in zip(per_device_arrays, local_devices)
    ]
    return device_buffers

  device_buffers = jax.tree_map(_put_to_devices, host_arrays)

  def _gda(global_shape, pspec, dbs):
    return gda_lib.GlobalDeviceArray(global_shape.shape, global_mesh, pspec,
                                     dbs)

  return jax.tree_map(_gda, global_shapes, pspecs, device_buffers)


def convert_fully_replicated_sda_to_gda(sda):
  """Convert a fully replicated SDA to GDA."""
  # SDA is fully replicated, so its device_buffers[0].shape is the global shape.
  global_shape = sda.device_buffers[0].shape
  # Create a 1D mesh to create fully replicated GDA.
  mesh = maps.Mesh(np.array(jax.devices()), axis_names=('x',))
  partition_spec = pjit.PartitionSpec(None)
  # pmap-produced SDA has a "scrambled" device order.
  return gda_lib.GlobalDeviceArray(
      global_shape, mesh, partition_spec,
      sorted(sda.device_buffers, key=lambda x: x.device().id))


def convert_fully_replicated_gda_to_sda(gda):
  """Convert a fully replicated GDA to SDA."""
  local_shape = (jax.local_device_count(),) + gda.shape
  local_aval = jax.core.ShapedArray(local_shape, gda.dtype)
  global_mesh = gda.mesh
  sharding_spec = pxla.mesh_sharding_specs(global_mesh.local_mesh.shape,
                                           global_mesh.local_mesh.axis_names)(
                                               local_aval, {})
  return pxla.make_sharded_device_array(local_aval, sharding_spec,
                                        list(gda._device_buffers))  # pylint: disable=protected-access


def get_global_input_shape_dtype(x: jnp.ndarray) -> jax.ShapeDtypeStruct:
  """Get global input shape/dtype assuming fully sharded batch dim."""
  assert len(x.shape) >= 1
  # Assume fully sharded batch dim.
  x_shape = (x.shape[0] * jax.process_count(),) + x.shape[1:]
  return jax.ShapeDtypeStruct(x_shape, x.dtype)


def set_globally_use_rbg_prng_key() -> None:
  """Must call this before any JAX computation to set RBG PRNGKey globally."""
  jax.config.update('jax_default_prng_impl', 'rbg')


def total_num_vars(variables) -> int:
  """Returns the total number of variables of the given variable collections."""
  param_shape_counts = jax.tree_map(lambda x: np.prod(x.shape), variables)
  flattened_counts, _ = jax.tree_util.tree_flatten(param_shape_counts)
  return np.sum(flattened_counts)


def global_mesh_defined() -> bool:
  """Checks if global xmap/pjit mesh resource environment is defined."""
  maps_env = jax.experimental.maps.thread_resources.env
  return maps_env.physical_mesh.devices.shape != ()  # pylint: disable=g-explicit-bool-comparison


# This wrapped with_sharding_constraint will not throw error for eval_shape
# outside pjit. It is also used in p5x.
def with_sharding_constraint(
    x: JTensor, axis_resources: Optional[pjit.PartitionSpec]) -> JTensor:
  """Wrapper for pjit with_sharding_constraint, no-op on cpu or outside pjit."""
  if jax.devices()[0].platform == 'cpu' or not global_mesh_defined():
    return x
  else:
    return pjit.with_sharding_constraint(x, axis_resources)


def get_uneven_sharding_paddings(
    partition_spec: pjit.PartitionSpec, shape: Sequence[int],
    mesh_shape: Sequence[int], mesh_axis_names: Sequence[str]) -> Sequence[int]:
  """Returns the padding size on each dimension due to uneven sharding."""
  axes_sizes = {}
  for size, name in zip(mesh_shape, mesh_axis_names):
    axes_sizes[name] = size
  paddings = []
  for axes, dim_size in zip(partition_spec, shape):
    if isinstance(axes, str):
      axes = [axes]
    partitions = int(np.prod([axes_sizes[axis] for axis in (axes or ())]))
    padding = (partitions - dim_size % partitions) % partitions
    paddings.append(padding)
  return paddings


def is_optax_masked_node(x: Any) -> bool:
  """Check whether the input is an instance of optax MaskedNode."""
  return isinstance(x, optax.MaskedNode)


def maybe_pad_uneven_sharding(x: JTensor, partition_spec: pjit.PartitionSpec,
                              shape: Sequence[int], mesh_shape: Sequence[int],
                              mesh_axis_names: Sequence[str]) -> JTensor:
  """Pads x to make it evenly shardable, if needed."""
  paddings = get_uneven_sharding_paddings(partition_spec, shape, mesh_shape,
                                          mesh_axis_names)
  if all([p == 0 for p in paddings]):
    return x
  # Annotate before pad to make sure they have the same sharding. (Pad does not
  # have the highest sharding propgation priority.)
  x = with_sharding_constraint(x, partition_spec)
  return jnp.pad(x, [[0, p] for p in paddings])


def maybe_slice_uneven_sharding(x: JTensor, partition_spec: pjit.PartitionSpec,
                                shape: Sequence[int]) -> JTensor:
  """Slices x to remove padding due to uneven sharding, if needed."""
  if is_optax_masked_node(x):
    return x
  if list(shape) == list(x.shape):
    return x
  if x.shape == (0,):
    return x
  x = jax.lax.slice(x, [0] * x.ndim, shape)
  # Annotate after slice to make sure they have the same sharding. (Slice does
  # not have the highest sharding propgation priority.)
  return with_sharding_constraint(x, partition_spec)


@contextlib.contextmanager
def logging_verbosity_level(level: str):
  prev_level = logging.get_verbosity()
  try:
    logging.set_verbosity(level)
    yield
  finally:
    logging.set_verbosity(prev_level)


def select_nodes_by_indices(indices, *trees):
  """Selects PyTree nodes from multiple trees and constructs new tree.

  Args:
    indices: PyTree with the same structure as other `trees`. The leaf nodes are
      indices to select nodes from `trees`
    *trees: PyTree with the same structure as `indices`.

  Returns:
    PyTree with the same structure with the arguments. For example, if tree
    nodes are accessible as `tree[key]`, each node in the return value is
    defined as `ret[key] = trees[indices[key]][key]`.
  """
  return jax.tree_map(lambda idx, *arrays: arrays[idx], indices, *trees)


Patterns = Union[str, re.Pattern, Iterable[Union[re.Pattern, str]]]


def match_variable_names(tree: NestedMap, patterns: Patterns) -> NestedMap:
  """Checks if a prefix key of each variable is matching to one of `patterns`.

  Args:
    tree: NestedMap to be matched against `patterns`.
    patterns: `re.Pattern`, `str` that can be compiled into `re.Pattern`, or an
      iterator of those.

  Returns:
    A nested map with the same structure as `tree`. Each node of the tree is
    a boolean flag denoting whether the prefix name of the variable is matching
    to one of `patterns`.
  """
  # Convert singleton to the list
  if isinstance(patterns, (str, re.Pattern)):
    patterns = [patterns]
  # Compile (`re.compile` acts as an identity func when p is `Pattern`)
  patterns = [re.compile(p) for p in patterns]

  var_prefix = extract_prefixed_keys_from_nested_map(tree)
  return jax.tree_map(
      lambda x: any(p.fullmatch(x) is not None for p in patterns), var_prefix)


def update_matched_variables(old_tree: NestedMap,
                             new_tree: NestedMap,
                             patterns: Patterns,
                             invert: bool = False) -> NestedMap:
  """Partially updates `old_tree` by `new_tree`.

  depending on patterns.

  This function tests whether variable names are matching to the given
  regexp patterns, and if so, replace the variable with the corresponding
  variable in `new_tree`.

  Args:
    old_tree: A nested map to be updated.
    new_tree: A nested map with the same structure as `old_tree` containing the
      updated values.
    patterns: Regular expression patterns (`str`, `re.Patterns`, or an iterator
      of those) that are used to determine whether the variable should be
      updated.
    invert: If True, condition on the variable names is inverted. i.e. only the
      variables that are not matching to `patterns` will be updated.

  Returns:
    An updated NestedMap
  """
  mask = match_variable_names(old_tree, patterns)  # True for update
  if invert:
    mask = jax.tree_map(lambda x: not x, mask)
  indices = jax.tree_map(lambda x: 1 if x else 0, mask)
  return select_nodes_by_indices(indices, old_tree, new_tree)


def l2_normalize(x: JTensor, axis: int = -1, epsilon: float = 1e-12) -> JTensor:
  """L2-normalize a Jax tensor along certain dimension."""
  norm = jnp.sqrt(jnp.sum(x * x, axis=axis, keepdims=True) + epsilon)
  return x / norm


def create_device_mesh(ici_mesh_shape: Sequence[int],
                       dcn_mesh_shape: Optional[Sequence[int]] = None):
  """Creates a single- or multi-slice device mesh from mesh shapes.

  Args:
    ici_mesh_shape: The mesh shape for a single slice, or for each slice in a
      multi-slice setting.
    dcn_mesh_shape: The mesh shape to use for between-slice parallelism. If
      None, creates a single-slice mesh.

  Returns:
    An ndarray of JAX devices.
  """
  if dcn_mesh_shape is not None and any(s > 1 for s in dcn_mesh_shape):
    try:
      device_mesh = mesh_utils.create_hybrid_device_mesh(
          ici_mesh_shape, dcn_mesh_shape)
    except AssertionError as e:
      raise ValueError('Setting a nontrivial dcn_mesh_shape requires multiple '
                       'slices') from e
  else:
    device_mesh = mesh_utils.create_device_mesh(ici_mesh_shape)
  logging.info('device_mesh: %s', device_mesh)
  return device_mesh


def get_large_negative_number(dtype: jnp.dtype) -> JTensor:
  """Returns a large negative value for the given dtype."""
  # -0.7 is a float64 in Jax. Explicit cast output to target dtype.
  if jnp.issubdtype(dtype, jnp.inexact):
    dtype_max = jnp.finfo(dtype).max
  elif jnp.issubdtype(dtype, jnp.integer):
    dtype_max = jnp.iinfo(dtype).max
  else:
    raise ValueError('Unsupported dtype for inputs.')
  return jnp.asarray(-0.7 * dtype_max, dtype=dtype)


def sequence_mask(lengths: Union[JTensor, Sequence[int]],
                  maxlen: int,
                  dtype=jnp.bool_) -> JTensor:
  """Creates a sequence mask where 1s are valid positions and 0s are padded.

  Args:
    lengths: A JTensor or Python list of integers.
    maxlen: A Python int.
    dtype: Output data type.

  Returns:
    [..., maxlen] of 0/1 JTensor where 1s are valid positions.
  """
  lengths = jnp.array(lengths)
  return (jnp.arange(maxlen)[jnp.newaxis, ...] <
          lengths[..., jnp.newaxis]).astype(dtype)


def sequence_paddings(lengths: Union[JTensor, Sequence[int]],
                      maxlen: int,
                      dtype=jnp.float32) -> JTensor:
  """Creates sequence paddings based on the lengths.

  Args:
    lengths: A JTensor or Python list of integers.
    maxlen: A Python int.
    dtype: Output data type.

  Returns:
    A 0/1 JTensor of shape [..., maxlen], in which 1 indicates paddings.
  """
  lengths = jnp.array(lengths)
  return (jnp.arange(maxlen)[jnp.newaxis, ...] >=
          lengths[..., jnp.newaxis]).astype(dtype)


def tree_unstack(tree: Any, axis: int) -> Sequence[Any]:
  """Extracts an axis' dimension to the list dimension of the output.

  Args:
    tree: PyTree which must have the above axis dimension with same size for
      all leaf nodes. All leafs must be one of (np.ndarray, jnp.ndarray) types.
    axis: int, the axis to extract into the list dimension. All leafs in the
      pytree must have this dimension and must have the same shape.

  Returns:
    A list of PyTrees with the `axis` dimension extracted. I.e., if
      tree_leaf.shape[axis] == N, then len(returned_list) == N.
  """
  leaves = jax.tree_util.tree_leaves(tree)
  if not leaves:
    return []

  if not all(isinstance(leaf, (jnp.ndarray, np.ndarray)) for leaf in leaves):
    raise ValueError('leaves must be either a pure numpy or jax ndarray')

  axis_size = leaves[0].shape[axis]
  if not all(
      leaf.ndim > axis and leaf.shape[axis] == axis_size for leaf in leaves):
    raise ValueError(f'all leaves must have x.ndim > {axis}'
                     f' and x.shape[{axis}] == {axis_size}')

  flat_pytrees = []
  for i in range(axis_size):
    flat_pytrees.append(jax.tree_map(lambda x: x.take(i, axis), tree))  # pylint: disable=cell-var-from-loop"

  return flat_pytrees

