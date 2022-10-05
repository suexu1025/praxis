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

"""Tests for base_layer."""

import dataclasses
import functools
from typing import Any
from absl.testing import absltest
from absl.testing import parameterized
from flax import linen as nn
import jax
import jax.numpy as jnp
from praxis import base_layer
from praxis import pax_fiddle
from praxis import test_utils


class Identity(base_layer.BaseLayer):
  """Layer for testing summary writing."""

  def setup(self):
    pass

  def __call__(self, x):
    self.add_summary('debug', x, verbosity=3)
    self.add_summary('info', x, verbosity=2)
    return x


class AddBias(base_layer.BaseLayer):
  """A layer that adds bias to an input tensor."""

  @nn.compact
  def __call__(self, x: base_layer.JTensor) -> base_layer.JTensor:
    var_p = base_layer.WeightHParams(
        shape=(x.shape[-1],), init=base_layer.WeightInit.Constant(0.))
    b = self.create_variable('b', var_hparams=var_p, trainable=True)
    x, b = self._cast_to_fprop_dtype((x, b))
    return x + b


class MultipleBiasLayer(base_layer.BaseLayer):
  """A dummy layer that adds multiple biases to an input tensor."""

  class HParams(base_layer.BaseLayer.HParams):
    """Attributes for MultipleBiasLayer.

    Attributes:
      num_child: number of independent child AddBias layers to test.
      num_children: number of children to be added into a self.create_children.
    """
    num_child: int = 0
    num_children: int = 0

  @nn.compact
  def __call__(self, x: base_layer.JTensor) -> base_layer.JTensor:
    p = self.hparams
    b_p = AddBias.HParams()
    for i in range(p.num_child):
      x = self.create_child(f'child_{i}', b_p)(x)
    layers = self.create_children('children', [b_p] * p.num_children)
    for layer in layers:
      x = layer(x)
    return x


class BaseLayerTest(test_utils.TestCase):

  def test_summary_same_input_name(self):
    a = jnp.array([1., 2.], dtype=jnp.float32)
    b = jnp.array([3., 4.], dtype=jnp.float32)
    with base_layer.JaxContext.new_context() as context:
      summary_dict = context.summary_dict
      summary_dict.add_summary('my_custom_summary', a,
                               base_layer.SummaryType.SCALAR)
      summary_dict.add_summary('my_custom_summary', b,
                               base_layer.SummaryType.SCALAR)

      for key in summary_dict.dict:
        summary_type = base_layer.get_summary_type_from_key(key)
        self.assertEqual(summary_type, base_layer.SummaryType.SCALAR)
      self.assertSameElements(
          list(summary_dict.dict.keys()),
          {'my_custom_summary_scalar', 'my_custom_summary1_scalar'})

  def test_layer_summary_verbosity_log(self):
    layer_p = Identity.HParams(name='test_identity')
    layer = base_layer.instantiate(layer_p)

    x = jnp.array([1., 2.], dtype=jnp.float32)
    init_vars = layer.init(jax.random.PRNGKey(0), x)
    _, updated_vars = layer.apply(init_vars, x, mutable=[base_layer.SUMMARIES])
    summaries = updated_vars[base_layer.SUMMARIES]

    self.assertIn('debug_scalar', summaries)
    self.assertIn('info_scalar', summaries)
    self.assertArraysEqual(x, summaries['debug_scalar'])
    self.assertArraysEqual(x, summaries['info_scalar'])

  def test_layer_summary_verbosity_no_log(self):
    context_p = base_layer.JaxContext.HParams(
        do_eval=True, summary_verbosity=2)
    with base_layer.JaxContext.new_context(hparams=context_p):
      layer_p = Identity.HParams(name='test_identity')
      layer = base_layer.instantiate(layer_p)

      x = jnp.array([1., 2.], dtype=jnp.float32)
      init_vars = layer.init(jax.random.PRNGKey(0), x)
      _, updated_vars = layer.apply(
          init_vars, x, mutable=[base_layer.SUMMARIES])
      summaries = updated_vars[base_layer.SUMMARIES]

    self.assertNotIn('debug_scalar', summaries)
    self.assertIn('info_scalar', summaries)
    self.assertArraysEqual(x, summaries['info_scalar'])

  @parameterized.named_parameters(
      ('log', 2, 2, True),
      ('no_log', 2, 3, False),
  )
  def test_global_summary_verbosity(
      self, ctx_verbosity, summary_verbosity, should_log):
    context_p = base_layer.JaxContext.HParams(
        do_eval=True, summary_verbosity=ctx_verbosity)
    with base_layer.JaxContext.new_context(hparams=context_p):
      summary = jnp.array([1., 2.], dtype=jnp.float32)
      base_layer.add_global_summary('test', summary,
                                    verbosity=summary_verbosity)
      all_summaries = base_layer.all_global_summaries()

      self.assertEqual('/test_scalar' in all_summaries, should_log)
      if should_log:
        self.assertArraysEqual(summary, all_summaries['/test_scalar'])

  def test_get_summary_base_type(self):
    self.assertEqual(
        base_layer.SummaryType.SCALAR,
        base_layer.get_summary_base_type(base_layer.SummaryType.SCALAR))
    self.assertEqual(
        base_layer.SummaryType.SCALAR,
        base_layer.get_summary_base_type(
            base_layer.SummaryType.AGGREGATE_SCALAR))
    self.assertEqual(
        base_layer.SummaryType.IMAGE,
        base_layer.get_summary_base_type(base_layer.SummaryType.IMAGE))
    self.assertEqual(
        base_layer.SummaryType.IMAGE,
        base_layer.get_summary_base_type(
            base_layer.SummaryType.AGGREGATE_IMAGE))
    self.assertEqual(
        base_layer.SummaryType.TEXT,
        base_layer.get_summary_base_type(
            base_layer.SummaryType.TEXT))

  @parameterized.parameters((0, 2), (3, 0), (1, 4))
  def test_layer_building_nn_compact(self, num_child: int, num_children: int):
    x = jnp.array([[0., 1.], [2., 3.]], dtype=jnp.float32)

    p = MultipleBiasLayer.HParams()
    p.name = 'multi_bias'
    p.num_child = num_child
    p.num_children = num_children
    layer = base_layer.instantiate(p)

    with base_layer.JaxContext.new_context():
      params = layer.init(jax.random.PRNGKey(0), x)

    flattened_params, _ = jax.tree_util.tree_flatten(params)
    self.assertLen(flattened_params, num_children + num_child)

    y = layer.apply(params, x)
    self.assertAllClose(x, y)

  def test_copy_base_hparams(self):

    class ChildLayer(base_layer.FiddleBaseLayer):
      pass

    class ParentLayer(base_layer.FiddleBaseLayer):
      child: Any = None
      child_tpl: pax_fiddle.Config = pax_fiddle.template_field(ChildLayer)

    config_factories = dict(
        hparams=base_layer.BaseLayer.HParams,
        fiddle=functools.partial(pax_fiddle.Config, base_layer.FiddleBaseLayer))
    for source_name, source_factory in config_factories.items():
      source = source_factory(
          dtype=jnp.float64,
          ici_mesh_shape=[2, 3, 4],
          params_init=base_layer.default_param_init())

      for target_name, target_factory in config_factories.items():
        with self.subTest(f'{source_name}_to_{target_name}'):
          target = target_factory(dtype=jnp.float16)
          base_layer._SharedBaseLayer.copy_base_hparams(source, target)
          self.assertEqual(target.dtype, jnp.float16)
          self.assertEqual(target.ici_mesh_shape, [2, 3, 4])

      with self.subTest(f'{source_name}_to_fiddle_subfield'):
        target_parent = pax_fiddle.Config(
            ParentLayer,
            dtype=jnp.float32,
            child=pax_fiddle.Config(ChildLayer, dtype=jnp.float16),
            child_tpl=pax_fiddle.Config(ChildLayer, dtype=jnp.int32))
        base_layer._SharedBaseLayer.copy_base_hparams(source, target_parent)
        self.assertEqual(target_parent.dtype, jnp.float32)
        self.assertEqual(target_parent.ici_mesh_shape, [2, 3, 4])
        self.assertEqual(target_parent.child.dtype, jnp.float16)
        self.assertEqual(target_parent.child.ici_mesh_shape, [2, 3, 4])
        self.assertEqual(target_parent.child_tpl.dtype, jnp.int32)
        self.assertIsNone(target_parent.child_tpl.ici_mesh_shape)

  def test_post_init_hparams(self):

    class HParamsChild(base_layer.BaseLayer):

      class HParams(base_layer.BaseLayer.HParams):
        x: int = 0

    class FiddleChild(base_layer.FiddleBaseLayer):
      x: int = 0

    for child_cls in (HParamsChild, FiddleChild):

      class HParamsParent(base_layer.BaseLayer):

        class HParams(base_layer.BaseLayer.HParams):

          child_tpl: child_cls.HParams = base_layer.sub_config_field(
              child_cls.HParams)

        def setup(self):
          child_tpl = self.hparams.child_tpl.clone()
          child_tpl.x += 2
          self.create_child('child', child_tpl)

        def __call__(self):
          return 0

      class FiddleParent(base_layer.FiddleBaseLayer):

        child_tpl: pax_fiddle.Config = base_layer.sub_config_field(
            child_cls.HParams)

        def setup(self):
          child_tpl = self.child_tpl.clone()
          child_tpl.x += 2
          self.create_child('child', child_tpl)

        def __call__(self):
          return 0

      for parent_cls in (HParamsParent, FiddleParent):

        with self.subTest(f'{parent_cls.__name__}_{child_cls.__name__}'):
          p = parent_cls.HParams(name='test')
          p.child_tpl = child_cls.HParams(x=5)
          layer = p.Instantiate()

          model = layer.bind(
              layer.init(jax.random.PRNGKey(0)),
              mutable=[base_layer.HYPER_PARAMS])
          model.post_init_hparams()
          hyper_params = jax.tree_map(
              lambda x: x.meta,
              model.variables[base_layer.HYPER_PARAMS],
              is_leaf=lambda x: isinstance(x, base_layer.WrappedHParams))

          self.assertEqual(hyper_params['_hparams'].dtype, jnp.float32)
          self.assertEqual(hyper_params['child']['_hparams'].dtype, jnp.float32)
          self.assertEqual(hyper_params['child']['_hparams'].x, 7)


class FiddleBaseLayerTest(test_utils.TestCase):

  @parameterized.parameters([
      dict(expected=None),
      dict(ici_mesh_shape=[1, 2], mesh_axis_names=['a', 'b'], expected=[1, 2]),
      dict(
          ici_mesh_shape=[1, 2],
          dcn_mesh_shape=[3, 4],
          mesh_axis_names=['a', 'b'],
          expected=[1 * 3, 2 * 4]),
  ])
  def test_mesh_shape_property(self, expected, **kwargs):
    layer = base_layer.FiddleBaseLayer(**kwargs)
    self.assertEqual(layer.mesh_shape, expected)

  def test_hparams_instance_stub(self):

    class Layer(base_layer.FiddleBaseLayer):
      x: int = 0

    layer = Layer(x=3, fprop_dtype=jnp.float16)

    hparams_stub = layer.hparams
    self.assertIsInstance(hparams_stub, base_layer._FiddleHParamsInstanceStub)
    self.assertEqual(hparams_stub.cls, Layer)
    self.assertEqual(hparams_stub.x, 3)
    self.assertEqual(hparams_stub.fprop_dtype, jnp.float16)
    self.assertEqual(hparams_stub.dtype, jnp.float32)

    cloned = hparams_stub.clone()
    self.assertIsInstance(cloned, pax_fiddle.Config)
    self.assertEqual(cloned.cls, Layer)
    self.assertEqual(cloned.__fn_or_cls__, Layer)
    self.assertEqual(cloned.x, 3)
    self.assertEqual(cloned.fprop_dtype, jnp.float16)
    self.assertEqual(cloned.dtype, jnp.float32)

  def test_hparams_class_stub(self):

    class Layer(base_layer.FiddleBaseLayer):
      x: int = 0

    class AnotherLayer(base_layer.FiddleBaseLayer):
      y: int = 0

    layer = Layer(x=3, fprop_dtype=jnp.float16)

    hparams_cls_stub = layer.HParams
    self.assertIsInstance(hparams_cls_stub, base_layer._FiddleHParamsClassStub)

    with self.subTest('call'):
      cfg = hparams_cls_stub(x=3, fprop_dtype=jnp.float16)
      self.assertIsInstance(cfg, pax_fiddle.Config)
      self.assertEqual(cfg.cls, Layer)
      self.assertEqual(cfg.__fn_or_cls__, Layer)
      self.assertEqual(cfg.x, 3)
      self.assertEqual(cfg.fprop_dtype, jnp.float16)
      self.assertEqual(cfg.dtype, jnp.float32)

    with self.subTest('sub_config_field'):
      field_descr = base_layer.sub_config_field(hparams_cls_stub)
      self.assertIsInstance(field_descr, dataclasses.Field)

    with self.subTest('instancecheck'):
      cfg = hparams_cls_stub(x=3, fprop_dtype=jnp.float16)
      self.assertIsInstance(cfg, Layer.HParams)
      self.assertIsInstance(pax_fiddle.Config(Layer), Layer.HParams)
      self.assertNotIsInstance(pax_fiddle.Config(AnotherLayer), Layer.HParams)
      self.assertNotIsInstance(pax_fiddle, AnotherLayer.HParams)
      self.assertNotIsInstance(123, Layer.HParams)

  def test_override_weight_sharding_hparams(self):

    class Layer(base_layer.FiddleBaseLayer):

      class WeightShardingHParams(
          base_layer.FiddleBaseLayer.WeightShardingHParams):
        x: int = 5

      class ActivationShardingHParams(
          base_layer.FiddleBaseLayer.ActivationShardingHParams):
        y: str = 'y'

    with self.subTest('construct_layer_directly'):
      layer = Layer()
      self.assertIsInstance(layer.weight_split_dims_mapping,
                            Layer.WeightShardingHParams)
      self.assertIsNone(layer.weight_split_dims_mapping.wt)
      self.assertEqual(layer.weight_split_dims_mapping.x, 5)
      self.assertIsInstance(layer.activation_split_dims_mapping,
                            Layer.ActivationShardingHParams)
      self.assertIsNone(layer.activation_split_dims_mapping.out)
      self.assertEqual(layer.activation_split_dims_mapping.y, 'y')

    with self.subTest('build_layer_from_fiddle_config'):
      cfg = pax_fiddle.Config(Layer)
      cfg.weight_split_dims_mapping.x = 12
      cfg.activation_split_dims_mapping.y = 'yellow'
      layer2 = pax_fiddle.build(cfg)
      self.assertEqual(layer2.weight_split_dims_mapping.x, 12)
      self.assertEqual(layer2.activation_split_dims_mapping.y, 'yellow')

  def test_hparam_is_instance_of_fdl_buildable(self):

    class Child(base_layer.FiddleBaseLayer):
      size: int = 5

    with self.assertRaisesRegex(
        ValueError, 'default value is a mutable instance of fdl.Buildable'):

      # Allowing the default value of `child_tpl` to be a Config object here
      # would be problematic, because that mutable default value would be
      # shared by all instances of Parent.  E.g., if `a` and `b` were two
      # instances of Parent that did not override `child_tpl`, then setting
      # `a.child_tpl.size = 20` would also modify `b.child_tpl.size` to be 20
      # (since `a.child_tpl is b.child_tpl`).  We therefore raise an exception,
      # indicating that the user should use a `default_factory` rather than a
      # default value.
      class Parent(base_layer.FiddleBaseLayer):
        child_tpl: pax_fiddle.Config = pax_fiddle.Config(Child, size=2)

      del Parent  # unused.


if __name__ == '__main__':
  absltest.main()
