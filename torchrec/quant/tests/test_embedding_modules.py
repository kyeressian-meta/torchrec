#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import unittest
from dataclasses import replace
from typing import Dict, List, Optional, Type

import hypothesis.strategies as st
import torch
from hypothesis import given, settings, Verbosity
from torchrec import inference as trec_infer
from torchrec.modules.embedding_configs import (
    DataType,
    EmbeddingBagConfig,
    EmbeddingConfig,
    PoolingType,
    QuantConfig,
)
from torchrec.modules.embedding_modules import (
    EmbeddingBagCollection,
    EmbeddingCollection,
)
from torchrec.quant.embedding_modules import (
    EmbeddingBagCollection as QuantEmbeddingBagCollection,
    EmbeddingCollection as QuantEmbeddingCollection,
    quant_prep_enable_quant_state_dict_split_scale_bias,
)
from torchrec.sparse.jagged_tensor import JaggedTensor, KeyedJaggedTensor, KeyedTensor


class EmbeddingBagCollectionTest(unittest.TestCase):
    def _asserting_same_embeddings(
        self,
        pooled_embeddings_1: KeyedTensor,
        pooled_embeddings_2: KeyedTensor,
        atol: float = 1e-08,
    ) -> None:
        self.assertEqual(
            set(pooled_embeddings_1.keys()), set(pooled_embeddings_2.keys())
        )
        for key in pooled_embeddings_1.keys():
            self.assertEqual(
                pooled_embeddings_1[key].shape, pooled_embeddings_2[key].shape
            )
            self.assertTrue(
                torch.allclose(
                    pooled_embeddings_1[key].cpu().float(),
                    pooled_embeddings_2[key].cpu().float(),
                    atol=atol,
                )
            )

    def _test_ebc(
        self,
        tables: List[EmbeddingBagConfig],
        features: KeyedJaggedTensor,
        quant_type: torch.dtype = torch.qint8,
        output_type: torch.dtype = torch.float,
        quant_state_dict_split_scale_bias: bool = False,
        per_table_weight_dtype: Optional[Dict[str, torch.dtype]] = None,
    ) -> None:
        ebc = EmbeddingBagCollection(tables=tables)
        if quant_state_dict_split_scale_bias:
            quant_prep_enable_quant_state_dict_split_scale_bias(ebc)

        embeddings = ebc(features)

        # test forward
        if not per_table_weight_dtype:
            ebc.qconfig = torch.quantization.QConfig(
                activation=torch.quantization.PlaceholderObserver.with_args(
                    dtype=output_type
                ),
                weight=torch.quantization.PlaceholderObserver.with_args(
                    dtype=quant_type
                ),
            )
        else:
            ebc.qconfig = QuantConfig(
                activation=torch.quantization.PlaceholderObserver.with_args(
                    dtype=output_type
                ),
                weight=torch.quantization.PlaceholderObserver.with_args(
                    dtype=quant_type
                ),
                per_table_weight_dtype=per_table_weight_dtype,
            )

        qebc = QuantEmbeddingBagCollection.from_float(ebc)
        quantized_embeddings = qebc(features)

        self.assertEqual(quantized_embeddings.values().dtype, output_type)

        self._asserting_same_embeddings(embeddings, quantized_embeddings, atol=0.1)

        # test state dict
        state_dict = ebc.state_dict()
        quantized_state_dict = qebc.state_dict()
        self.assertTrue(
            set(state_dict.keys()).issubset(set(quantized_state_dict.keys()))
        )

    # pyre-fixme[56]
    @given(
        data_type=st.sampled_from(
            [
                DataType.FP32,
                DataType.FP16,
            ]
        ),
        quant_type=st.sampled_from(
            [
                torch.half,
                torch.qint8,
            ]
        ),
        output_type=st.sampled_from(
            [
                torch.half,
                torch.float,
            ]
        ),
        permute_order=st.booleans(),
        quant_state_dict_split_scale_bias=st.booleans(),
        per_table_weight_dtype=st.sampled_from(
            [
                {"t1": torch.quint4x2, "t2": torch.qint8},
                {"t1": torch.qint8, "t2": torch.quint4x2},
            ]
        ),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=8, deadline=None)
    def test_ebc(
        self,
        data_type: DataType,
        quant_type: torch.dtype,
        output_type: torch.dtype,
        permute_order: bool,
        quant_state_dict_split_scale_bias: bool,
        per_table_weight_dtype: Dict[str, torch.dtype],
    ) -> None:
        eb1_config = EmbeddingBagConfig(
            name="t1",
            embedding_dim=16,
            num_embeddings=10,
            feature_names=["f1"],
            data_type=data_type,
        )
        eb1_mean_config = replace(
            eb1_config,
            name="t1_mean",
            pooling=PoolingType.MEAN,
            embedding_dim=32,
        )
        eb2_config = replace(eb1_config, name="t2", feature_names=["f2"])
        features = (
            KeyedJaggedTensor(
                keys=["f1", "f2"],
                values=torch.as_tensor([0, 2, 1, 3]),
                lengths=torch.as_tensor([1, 1, 2, 0]),
            )
            if not permute_order
            else KeyedJaggedTensor(
                keys=["f2", "f1"],
                values=torch.as_tensor([1, 3, 0, 2]),
                lengths=torch.as_tensor([2, 0, 1, 1]),
            )
        )
        # The key for grouping tables is (pooling, data_type).  Test having a different
        # key value in the middle.
        self._test_ebc(
            [eb1_config, eb1_mean_config, eb2_config],
            features,
            quant_type,
            output_type,
            quant_state_dict_split_scale_bias,
        )

        self._test_ebc(
            [eb1_config, eb1_mean_config, eb2_config],
            features,
            quant_type,
            output_type,
            quant_state_dict_split_scale_bias,
            per_table_weight_dtype,
        )

    def test_create_on_meta_device_without_providing_weights(self) -> None:
        emb_bag = EmbeddingBagConfig(
            name="t1",
            embedding_dim=16,
            num_embeddings=10,
            feature_names=["f1"],
        )
        QuantEmbeddingBagCollection(
            [emb_bag], is_weighted=False, device=torch.device("meta")
        )
        emb = EmbeddingConfig(
            name="t1",
            embedding_dim=16,
            num_embeddings=10,
            feature_names=["f1"],
        )
        QuantEmbeddingCollection([emb], device=torch.device("meta"))

    def test_shared_tables(self) -> None:
        eb_config = EmbeddingBagConfig(
            name="t1", embedding_dim=16, num_embeddings=10, feature_names=["f1", "f2"]
        )
        features = KeyedJaggedTensor(
            keys=["f1", "f2"],
            values=torch.as_tensor([0, 1]),
            lengths=torch.as_tensor([1, 1]),
        )
        self._test_ebc([eb_config], features)

    def test_shared_features(self) -> None:
        eb1_config = EmbeddingBagConfig(
            name="t1", embedding_dim=16, num_embeddings=10, feature_names=["f1"]
        )
        eb2_config = EmbeddingBagConfig(
            name="t2", embedding_dim=16, num_embeddings=10, feature_names=["f1"]
        )
        features = KeyedJaggedTensor(
            keys=["f1"],
            values=torch.as_tensor([0, 1]),
            lengths=torch.as_tensor([1, 1]),
        )
        self._test_ebc([eb1_config, eb2_config], features)

    def test_multiple_features(self) -> None:
        eb1_config = EmbeddingBagConfig(
            name="t1", embedding_dim=16, num_embeddings=10, feature_names=["f1", "f2"]
        )
        eb2_config = EmbeddingBagConfig(
            name="t2", embedding_dim=16, num_embeddings=10, feature_names=["f3"]
        )
        features = KeyedJaggedTensor(
            keys=["f1", "f2", "f3"],
            values=torch.as_tensor([0, 1, 2]),
            lengths=torch.as_tensor([1, 1, 1]),
        )
        self._test_ebc([eb1_config, eb2_config], features)

    # pyre-ignore
    @given(
        data_type=st.sampled_from(
            [
                DataType.FP32,
                DataType.FP16,
            ]
        ),
        quant_type=st.sampled_from(
            [
                torch.half,
                torch.qint8,
            ]
        ),
        output_type=st.sampled_from(
            [
                torch.half,
                torch.float,
            ]
        ),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=8, deadline=None)
    def test_save_load_state_dict(
        self,
        data_type: DataType,
        quant_type: torch.dtype,
        output_type: torch.dtype,
    ) -> None:
        eb1_config = EmbeddingBagConfig(
            name="t1",
            embedding_dim=16,
            num_embeddings=10,
            feature_names=["f1"],
            data_type=data_type,
        )
        eb2_config = EmbeddingBagConfig(
            name="t2",
            embedding_dim=16,
            num_embeddings=10,
            feature_names=["f1"],
            data_type=data_type,
        )
        tables = [eb1_config, eb2_config]

        ebc = EmbeddingBagCollection(tables=tables)

        # test forward
        ebc.qconfig = torch.quantization.QConfig(
            activation=torch.quantization.PlaceholderObserver.with_args(
                dtype=output_type
            ),
            weight=torch.quantization.PlaceholderObserver.with_args(dtype=quant_type),
        )

        qebc = QuantEmbeddingBagCollection.from_float(ebc)
        qebc_state_dict = qebc.state_dict()

        ebc_2 = EmbeddingBagCollection(tables=tables)
        ebc_2.qconfig = torch.quantization.QConfig(
            activation=torch.quantization.PlaceholderObserver.with_args(
                dtype=output_type
            ),
            weight=torch.quantization.PlaceholderObserver.with_args(dtype=quant_type),
        )

        qebc_2 = QuantEmbeddingBagCollection.from_float(ebc_2)

        qebc_2.load_state_dict(qebc_state_dict)
        qebc_2_state_dict = qebc_2.state_dict()

        for key in qebc_state_dict:
            torch.testing.assert_close(qebc_state_dict[key], qebc_2_state_dict[key])

        features = KeyedJaggedTensor(
            keys=["f1", "f2"],
            values=torch.as_tensor([0, 1]),
            lengths=torch.as_tensor([1, 1]),
        )

        embeddings = qebc(features)
        embeddings_2 = qebc_2(features)
        self._asserting_same_embeddings(embeddings, embeddings_2)

    # pyre-ignore
    @given(
        data_type=st.sampled_from(
            [
                DataType.FP32,
                DataType.FP16,
            ]
        ),
        quant_type=st.sampled_from(
            [
                torch.half,
                torch.qint8,
            ]
        ),
        output_type=st.sampled_from(
            [
                torch.half,
                torch.float,
            ]
        ),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=8, deadline=None)
    def test_composability(
        self,
        data_type: DataType,
        quant_type: torch.dtype,
        output_type: torch.dtype,
    ) -> None:
        class TestModel(torch.nn.Module):
            def __init__(self, ebc: EmbeddingBagCollection) -> None:
                super().__init__()
                self.ebc = ebc
                self.over_arch = torch.nn.Linear(
                    16,
                    1,
                )

            def forward(self, kjt: KeyedJaggedTensor) -> torch.Tensor:
                ebc_output = self.ebc.forward(kjt).to_dict()
                sparse_features = []
                for key in kjt.keys():
                    sparse_features.append(ebc_output[key])
                sparse_features = torch.cat(sparse_features, dim=0)
                return self.over_arch(sparse_features)

        eb1_config = EmbeddingBagConfig(
            name="t1",
            embedding_dim=16,
            num_embeddings=10,
            feature_names=["f1"],
            data_type=data_type,
        )
        eb2_config = EmbeddingBagConfig(
            name="t2",
            embedding_dim=16,
            num_embeddings=10,
            feature_names=["f1"],
            data_type=data_type,
        )
        tables = [eb1_config, eb2_config]

        ebc = EmbeddingBagCollection(tables=tables)
        # test forward
        ebc.qconfig = torch.quantization.QConfig(
            activation=torch.quantization.PlaceholderObserver.with_args(
                dtype=output_type
            ),
            weight=torch.quantization.PlaceholderObserver.with_args(dtype=quant_type),
        )

        test_model = TestModel(ebc)

        before_quant_state_dict = test_model.state_dict()
        test_model.ebc = QuantEmbeddingBagCollection.from_float(ebc)

        state_dict = test_model.state_dict()
        self.assertTrue(
            set(before_quant_state_dict.keys()).issubset(set(state_dict.keys()))
        )
        test_model.load_state_dict(state_dict)

    def test_trace_and_script(self) -> None:
        data_type = DataType.FP16
        quant_type = torch.half
        output_type = torch.half

        eb1_config = EmbeddingBagConfig(
            name="t1",
            embedding_dim=16,
            num_embeddings=10,
            feature_names=["f1"],
            data_type=data_type,
        )
        eb2_config = EmbeddingBagConfig(
            name="t2",
            embedding_dim=16,
            num_embeddings=10,
            feature_names=["f1"],
            data_type=data_type,
        )

        ebc = EmbeddingBagCollection(tables=[eb1_config, eb2_config])
        ebc.qconfig = torch.quantization.QConfig(
            activation=torch.quantization.PlaceholderObserver.with_args(
                dtype=output_type
            ),
            weight=torch.quantization.PlaceholderObserver.with_args(dtype=quant_type),
        )

        qebc = QuantEmbeddingBagCollection.from_float(ebc)

        from torchrec.fx import symbolic_trace

        gm = symbolic_trace(qebc)

        features = KeyedJaggedTensor(
            keys=["f1", "f2"],
            values=torch.as_tensor([0, 1]),
            lengths=torch.as_tensor([1, 1]),
        )

        original_out = qebc(features)
        traced_out = gm(features)

        scripted_module = torch.jit.script(gm)
        scripted_out = scripted_module(features)

        self.assertEqual(original_out.keys(), traced_out.keys())
        torch.testing.assert_close(original_out.values(), traced_out.values())
        self.assertEqual(original_out.offset_per_key(), traced_out.offset_per_key())

        self.assertEqual(original_out.keys(), scripted_out.keys())
        torch.testing.assert_close(original_out.values(), scripted_out.values())
        self.assertEqual(original_out.offset_per_key(), scripted_out.offset_per_key())


class EmbeddingCollectionTest(unittest.TestCase):
    def _comp_ec_output(
        self,
        embeddings: Dict[str, JaggedTensor],
        transformed_graph_embeddings: Dict[str, JaggedTensor],
        atol: int = 1,
    ) -> None:
        self.assertEqual(embeddings.keys(), transformed_graph_embeddings.keys())
        for key in embeddings.keys():
            self.assertEqual(
                embeddings[key].values().size(),
                transformed_graph_embeddings[key].values().size(),
            )
            self.assertTrue(
                torch.allclose(
                    embeddings[key].values().cpu().float(),
                    transformed_graph_embeddings[key].values().cpu().float(),
                    atol=atol,
                )
            )

    def _test_ec(
        self,
        tables: List[EmbeddingConfig],
        features: KeyedJaggedTensor,
        quant_type: torch.dtype = torch.qint8,
        output_type: torch.dtype = torch.float,
        quant_state_dict_split_scale_bias: bool = False,
    ) -> None:
        ec = EmbeddingCollection(tables=tables)
        if quant_state_dict_split_scale_bias:
            quant_prep_enable_quant_state_dict_split_scale_bias(ec)

        embeddings = ec(features)

        # test forward
        ec.qconfig = torch.quantization.QConfig(
            activation=torch.quantization.PlaceholderObserver.with_args(
                dtype=output_type
            ),
            weight=torch.quantization.PlaceholderObserver.with_args(dtype=quant_type),
        )

        qec = QuantEmbeddingCollection.from_float(ec)
        quantized_embeddings = qec(features)
        self.assertEqual(
            list(quantized_embeddings.values())[0].values().dtype, output_type
        )
        self._comp_ec_output(embeddings, quantized_embeddings)

        # test state dict
        state_dict = ec.state_dict()
        quantized_state_dict = ec.state_dict()
        self.assertEqual(state_dict.keys(), quantized_state_dict.keys())

    # pyre-fixme[56]
    @given(
        data_type=st.sampled_from(
            [
                DataType.FP32,
                DataType.INT8,
            ]
        ),
        quant_type=st.sampled_from(
            [
                torch.half,
                torch.qint8,
            ]
        ),
        output_type=st.sampled_from(
            [
                torch.half,
                torch.float,
            ]
        ),
        quant_state_dict_split_scale_bias=st.booleans(),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=2, deadline=None)
    def test_ec(
        self,
        data_type: DataType,
        quant_type: torch.dtype,
        output_type: torch.dtype,
        quant_state_dict_split_scale_bias: bool,
    ) -> None:
        eb1_config = EmbeddingConfig(
            name="t1",
            embedding_dim=16,
            num_embeddings=10,
            feature_names=["f1", "f2"],
            data_type=data_type,
        )
        eb2_config = EmbeddingConfig(
            name="t2",
            embedding_dim=16,
            num_embeddings=10,
            feature_names=["f3", "f4"],
            data_type=data_type,
        )
        features = KeyedJaggedTensor(
            keys=["f1", "f2", "f3", "f4"],
            values=torch.as_tensor(
                [
                    5,
                    1,
                    0,
                    0,
                    4,
                    3,
                    4,
                    9,
                    2,
                    2,
                    3,
                    3,
                    1,
                    5,
                    0,
                    7,
                    5,
                    0,
                    9,
                    9,
                    3,
                    5,
                    6,
                    6,
                    9,
                    3,
                    7,
                    8,
                    7,
                    7,
                    9,
                    1,
                    2,
                    6,
                    7,
                    6,
                    1,
                    8,
                    3,
                    8,
                    1,
                    9,
                ]
            ),
            lengths=torch.as_tensor([9, 12, 9, 12]),
        )
        self._test_ec(
            tables=[eb1_config, eb2_config],
            features=features,
            quant_state_dict_split_scale_bias=quant_state_dict_split_scale_bias,
        )

    def test_shared_tables(self) -> None:
        eb_config = EmbeddingConfig(
            name="t1", embedding_dim=16, num_embeddings=10, feature_names=["f1", "f2"]
        )
        features = KeyedJaggedTensor(
            keys=["f1", "f2"],
            values=torch.as_tensor([0, 1]),
            lengths=torch.as_tensor([1, 1]),
        )
        self._test_ec([eb_config], features)

    def test_shared_features(self) -> None:
        eb1_config = EmbeddingConfig(
            name="t1", embedding_dim=16, num_embeddings=10, feature_names=["f1"]
        )
        eb2_config = EmbeddingConfig(
            name="t2", embedding_dim=16, num_embeddings=10, feature_names=["f1"]
        )
        features = KeyedJaggedTensor(
            keys=["f1"],
            values=torch.as_tensor([0, 1]),
            lengths=torch.as_tensor([1, 1]),
        )
        self._test_ec([eb1_config, eb2_config], features)

    def test_different_quantization_dtype_per_ec_table(self) -> None:
        class TestModule(torch.nn.Module):
            def __init__(self, m: torch.nn.Module) -> None:
                super().__init__()
                self.m = m

        eb1_config = EmbeddingConfig(
            name="t1", embedding_dim=16, num_embeddings=10, feature_names=["f1"]
        )
        eb2_config = EmbeddingConfig(
            name="t2", embedding_dim=16, num_embeddings=10, feature_names=["f1"]
        )
        ec = EmbeddingCollection(tables=[eb1_config, eb2_config])
        model = TestModule(ec)
        qconfig_spec_keys: List[Type[torch.nn.Module]] = [EmbeddingCollection]
        quant_mapping: Dict[Type[torch.nn.Module], Type[torch.nn.Module]] = {
            EmbeddingCollection: QuantEmbeddingCollection
        }
        trec_infer.modules.quantize_embeddings(
            model,
            dtype=torch.int8,
            additional_qconfig_spec_keys=qconfig_spec_keys,
            additional_mapping=quant_mapping,
            inplace=True,
            per_table_weight_dtype={"t1": torch.float16},
        )
        configs = model.m.embedding_configs()
        self.assertEqual(len(configs), 2)
        self.assertNotEqual(configs[0].name, configs[1].name)
        for config in configs:
            if config.name == "t1":
                self.assertEqual(config.data_type, DataType.FP16)
            else:
                self.assertEqual(config.name, "t2")
                self.assertEqual(config.data_type, DataType.INT8)

    def test_different_quantization_dtype_per_ebc_table(self) -> None:
        class TestModule(torch.nn.Module):
            def __init__(self, m: torch.nn.Module) -> None:
                super().__init__()
                self.m = m

        eb1_config = EmbeddingBagConfig(
            name="t1", embedding_dim=16, num_embeddings=10, feature_names=["f1"]
        )
        eb2_config = EmbeddingBagConfig(
            name="t2", embedding_dim=16, num_embeddings=10, feature_names=["f1"]
        )
        ebc = EmbeddingBagCollection(tables=[eb1_config, eb2_config])
        model = TestModule(ebc)
        trec_infer.modules.quantize_embeddings(
            model,
            dtype=torch.int8,
            inplace=True,
            per_table_weight_dtype={"t1": torch.float16},
        )
        configs = model.m.embedding_bag_configs()
        self.assertEqual(len(configs), 2)
        self.assertNotEqual(configs[0].name, configs[1].name)
        for config in configs:
            if config.name == "t1":
                self.assertEqual(config.data_type, DataType.FP16)
            else:
                self.assertEqual(config.name, "t2")
                self.assertEqual(config.data_type, DataType.INT8)

    def test_trace_and_script(self) -> None:
        data_type = DataType.FP16
        quant_type = torch.half
        output_type = torch.half

        ec1_config = EmbeddingConfig(
            name="t1",
            embedding_dim=16,
            num_embeddings=10,
            feature_names=["f1", "f2"],
            data_type=data_type,
        )
        ec2_config = EmbeddingConfig(
            name="t2",
            embedding_dim=16,
            num_embeddings=10,
            feature_names=["f3", "f4"],
            data_type=data_type,
        )

        ec = EmbeddingCollection(tables=[ec1_config, ec2_config])
        ec.qconfig = torch.quantization.QConfig(
            activation=torch.quantization.PlaceholderObserver.with_args(
                dtype=output_type
            ),
            weight=torch.quantization.PlaceholderObserver.with_args(dtype=quant_type),
        )

        qec = QuantEmbeddingCollection.from_float(ec)

        from torchrec.fx import symbolic_trace

        gm = symbolic_trace(qec)

        features = KeyedJaggedTensor(
            keys=["f1", "f2", "f3", "f4"],
            values=torch.as_tensor([0, 1, 2, 3, 4, 5, 6, 7]),
            lengths=torch.as_tensor([1, 2, 3, 2]),
        )

        original_out = qec(features)
        traced_out = gm(features)

        scripted_module = torch.jit.script(gm)
        scripted_out = scripted_module(features)
        self._comp_ec_output(original_out, traced_out, atol=0)
        self._comp_ec_output(original_out, scripted_out, atol=0)
