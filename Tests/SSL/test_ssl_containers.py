#  ------------------------------------------------------------------------------------------
#  Copyright (c) Microsoft Corporation. All rights reserved.
#  Licensed under the MIT License (MIT). See LICENSE in the repo root for license information.
#  ------------------------------------------------------------------------------------------
from pathlib import Path
from unittest import mock

import math
import numpy as np
import pandas as pd
import pytest
import torch
from pl_bolts.models.self_supervised.resnets import ResNet
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import ModelCheckpoint
from torch.nn import Module
from torch.optim.lr_scheduler import _LRScheduler
from typing import Dict

from InnerEye.Common import fixed_paths
from InnerEye.Common.common_util import is_windows
from InnerEye.Common.fixed_paths import repository_root_directory
from InnerEye.Common.fixed_paths_for_tests import full_ml_test_data_path
from InnerEye.Common.output_directories import OutputFolderForTests
from InnerEye.ML.SSL.lightning_containers.ssl_container import EncoderName, SSLDatasetName
from InnerEye.ML.SSL.lightning_modules.byol.byol_module import BYOLInnerEye
from InnerEye.ML.SSL.lightning_modules.simclr_module import SimCLRInnerEye
from InnerEye.ML.SSL.lightning_modules.ssl_classifier_module import SSLClassifier
from InnerEye.ML.SSL.lightning_modules.ssl_online_evaluator import SSLOnlineEvaluatorInnerEye
from InnerEye.ML.SSL.utils import SSLDataModuleType, SSLTrainingType
from InnerEye.ML.common import BEST_CHECKPOINT_FILE_NAME_WITH_SUFFIX
from InnerEye.ML.configs.ssl.CXR_SSL_configs import CXRImageClassifier
from InnerEye.ML.runner import Runner
from Tests.ML.configs.lightning_test_containers import DummyContainerWithModel
from Tests.ML.utils.test_io_util import write_test_dicom

path_to_test_dataset = full_ml_test_data_path("cxr_test_dataset")


def _create_test_cxr_data(path_to_test_dataset: Path) -> None:
    """
    Creates fake datasets dataframe and dicom images mimicking the expected structure of the datasets
    of NIHCXR and RSNAKaggleCXR
    :param path_to_test_dataset: folder to which we want to save the mock data.
    """
    if path_to_test_dataset.exists():
        return
    path_to_test_dataset.mkdir(exist_ok=True)
    df = pd.DataFrame({"Image Index": np.repeat("1.dcm", 200)})
    df.to_csv(path_to_test_dataset / "Data_Entry_2017.csv", index=False)
    df = pd.DataFrame({"subject": np.repeat("1", 300),
                       "label": np.random.RandomState(42).binomial(n=1, p=0.2, size=300)})
    df.to_csv(path_to_test_dataset / "dataset.csv", index=False)
    write_test_dicom(array=np.ones([256, 256], dtype="uint16"), path=path_to_test_dataset / "1.dcm")


def default_runner() -> Runner:
    """
    Create an InnerEye Runner object with the default settings, pointing to the repository root and
    default settings files.
    """
    return Runner(project_root=repository_root_directory(),
                  yaml_config_file=fixed_paths.SETTINGS_YAML_FILE)


common_test_args = ["", "--is_debug_model=True", "--num_epochs=1", "--ssl_training_batch_size=10",
                    "--linear_head_batch_size=5",
                    "--num_workers=0"]


def _compare_stored_metrics(runner: Runner, expected_metrics: Dict[str, float], abs: float = 1e-5) -> None:
    """
    Checks if the StoringLogger in the given runner holds all the expected metrics as results of training
    epoch 0, up to a given absolute precision.
    :param runner: The Innereye runner.
    :param expected_metrics: A dictionary with all metrics that are expected to be present.
    """
    assert runner.ml_runner is not None
    assert runner.ml_runner.storing_logger is not None
    print(f"Actual metrics in epoch 0: {runner.ml_runner.storing_logger.results_per_epoch[0]}")
    print(f"Expected metrics: {expected_metrics}")
    for metric, expected in expected_metrics.items():
        actual = runner.ml_runner.storing_logger.results_per_epoch[0][metric]
        if isinstance(actual, float):
            if math.isnan(expected):
                assert math.isnan(actual), f"Metric {metric}: Expected NaN, but got: {actual}"
            else:
                assert actual == pytest.approx(expected, abs=abs), f"Mismatch for metric {metric}"
        else:
            assert actual == expected, f"Mismatch for metric {metric}"


@pytest.mark.skipif(is_windows(), reason="Too slow on windows")
def test_innereye_ssl_container_cifar10_resnet_simclr() -> None:
    """
    Tests:
        - training of SSL model on cifar10 for one epoch
        - checkpoint saving
        - checkpoint loading and ImageClassifier module creation
        - training of image classifier for one epoch.
    """
    args = common_test_args + ["--model=CIFAR10SimCLR"]
    runner = default_runner()
    with mock.patch("sys.argv", args):
        loaded_config, actual_run = runner.run()
    assert loaded_config is not None
    assert isinstance(loaded_config.model, SimCLRInnerEye)
    assert loaded_config.encoder_output_dim == 2048
    assert loaded_config.l_rate == 1e-4
    assert loaded_config.num_epochs == 1
    assert loaded_config.recovery_checkpoint_save_interval == 200
    assert loaded_config.ssl_training_type == SSLTrainingType.SimCLR
    assert loaded_config.online_eval.num_classes == 10
    assert loaded_config.online_eval.dataset == SSLDatasetName.CIFAR10.value
    assert loaded_config.ssl_training_dataset_name == SSLDatasetName.CIFAR10
    assert not loaded_config.use_balanced_binary_loss_for_linear_head
    assert isinstance(loaded_config.model.encoder.cnn_model, ResNet)

    # Check the metrics that were recorded during training
    expected_metrics = {
        'simclr/train/loss': 3.423144578933716,
        'simclr/learning_rate': 0.0,
        'ssl_online_evaluator/train/loss': 2.6143882274627686,
        'ssl_online_evaluator/train/online_AccuracyAtThreshold05': 0.0,
        'epoch_started': 0.0,
        'simclr/val/loss': 2.886892795562744,
        'ssl_online_evaluator/val/loss': 2.2472469806671143,
        'ssl_online_evaluator/val/AccuracyAtThreshold05': 0.20000000298023224
    }
    _compare_stored_metrics(runner, expected_metrics, abs=5e-5)

    # Check that the checkpoint contains both the optimizer for the embedding and for the linear head
    checkpoint_path = loaded_config.outputs_folder / "checkpoints" / "best_checkpoint.ckpt"
    checkpoint = torch.load(checkpoint_path)
    assert len(checkpoint["optimizer_states"]) == 1
    assert len(checkpoint["lr_schedulers"]) == 1
    assert "callbacks" in checkpoint
    callback_name = SSLOnlineEvaluatorInnerEye.__name__
    assert callback_name in checkpoint["callbacks"]
    callback_state = checkpoint["callbacks"][callback_name]
    assert SSLOnlineEvaluatorInnerEye.OPTIMIZER_STATE_NAME in callback_state
    assert SSLOnlineEvaluatorInnerEye.EVALUATOR_STATE_NAME in callback_state

    # Now run the actual SSL classifier off the stored checkpoint
    args = common_test_args + ["--model=SSLClassifierCIFAR", f"--local_ssl_weights_path={checkpoint_path}"]
    with mock.patch("sys.argv", args):
        loaded_config, actual_run = default_runner().run()
    assert loaded_config is not None
    assert isinstance(loaded_config.model, SSLClassifier)
    assert loaded_config.model.class_weights is None
    assert loaded_config.model.num_classes == 10


@pytest.mark.skipif(is_windows(), reason="Too slow on windows")
def test_load_innereye_ssl_container_cifar10_cifar100_resnet_byol() -> None:
    """
    Tests that the parameters feed into the BYOL model and online evaluator are
    indeed the one we fed through our command line args
    """
    args = common_test_args + ["--model=CIFAR10CIFAR100BYOL"]
    runner = default_runner()
    with mock.patch("sys.argv", args):
        runner.parse_and_load_model()
    loaded_config = runner.lightning_container
    assert loaded_config is not None
    assert loaded_config.linear_head_dataset_name == SSLDatasetName.CIFAR100
    assert loaded_config.ssl_training_dataset_name == SSLDatasetName.CIFAR10
    assert loaded_config.ssl_training_type == SSLTrainingType.BYOL


@pytest.mark.skipif(is_windows(), reason="Too slow on windows")
def test_innereye_ssl_container_rsna() -> None:
    """
    Test if we can get the config loader to load a Lightning container model, and then train locally.
    """
    runner = default_runner()
    _create_test_cxr_data(path_to_test_dataset)
    # Test training of SSL model
    args = common_test_args + ["--model=NIH_RSNA_BYOL",
                               f"--local_dataset={str(path_to_test_dataset)}",
                               f"--extra_local_dataset_paths={str(path_to_test_dataset)}",
                               "--use_balanced_binary_loss_for_linear_head=True",
                               f"--ssl_encoder={EncoderName.densenet121.value}"]
    with mock.patch("sys.argv", args):
        loaded_config, actual_run = runner.run()
    assert loaded_config is not None
    assert isinstance(loaded_config.model, BYOLInnerEye)
    assert loaded_config.online_eval.dataset == SSLDatasetName.RSNAKaggleCXR.value
    assert loaded_config.online_eval.num_classes == 2
    assert loaded_config.ssl_training_dataset_name == SSLDatasetName.NIHCXR
    assert loaded_config.ssl_training_type == SSLTrainingType.BYOL
    assert loaded_config.encoder_output_dim == 1024  # DenseNet output size
    # Check model params
    assert isinstance(loaded_config.model.hparams, Dict)
    assert loaded_config.model.hparams["batch_size"] == 10
    assert loaded_config.model.hparams["use_7x7_first_conv_in_resnet"]
    assert loaded_config.model.hparams["encoder_name"] == EncoderName.densenet121.value
    assert loaded_config.model.hparams["learning_rate"] == 1e-4
    assert loaded_config.model.hparams["num_samples"] == 180

    # Check some augmentation params
    assert loaded_config.datamodule_args[
               SSLDataModuleType.ENCODER].augmentation_params.preprocess.center_crop_size == 224
    assert loaded_config.datamodule_args[SSLDataModuleType.ENCODER].augmentation_params.augmentation.use_random_crop
    assert loaded_config.datamodule_args[SSLDataModuleType.ENCODER].augmentation_params.augmentation.use_random_affine

    expected_metrics = {
        'byol/train/loss': 0.00401744619011879,
        'byol/tau': 0.9899999499320984,
        'byol/learning_rate/0/0': 0.0,
        'byol/learning_rate/0/1': 0.0,
        'ssl_online_evaluator/train/loss': 0.685592532157898,
        'ssl_online_evaluator/train/online_AreaUnderRocCurve': 0.5,
        'ssl_online_evaluator/train/online_AreaUnderPRCurve': 0.699999988079071,
        'ssl_online_evaluator/train/online_AccuracyAtThreshold05': 0.4000000059604645,
        'epoch_started': 0.0,
        'byol/val/loss': -0.07644838094711304,
        'ssl_online_evaluator/val/loss': 0.6965796947479248,
        'ssl_online_evaluator/val/AreaUnderRocCurve': math.nan,
        'ssl_online_evaluator/val/AreaUnderPRCurve': math.nan,
        'ssl_online_evaluator/val/AccuracyAtThreshold05': 0.0
    }
    _compare_stored_metrics(runner, expected_metrics)

    # Check that we are able to load the checkpoint and create classifier model
    checkpoint_path = loaded_config.checkpoint_folder / BEST_CHECKPOINT_FILE_NAME_WITH_SUFFIX
    args = common_test_args + ["--model=CXRImageClassifier",
                               f"--local_dataset={str(path_to_test_dataset)}",
                               "--use_balanced_binary_loss_for_linear_head=True",
                               f"--local_ssl_weights_path={checkpoint_path}"]
    with mock.patch("sys.argv", args):
        loaded_config, actual_run = runner.run()
    assert loaded_config is not None
    assert isinstance(loaded_config, CXRImageClassifier)
    assert loaded_config.model.freeze_encoder
    assert torch.isclose(loaded_config.model.class_weights, torch.tensor([0.21, 0.79]), atol=1e-6).all()  # type: ignore
    assert loaded_config.model.num_classes == 2


def test_simclr_lr_scheduler() -> None:
    """
    Test if the LR scheduler has the expected warmup behaviour.
    """
    num_samples = 100
    batch_size = 20
    gpus = 1
    max_epochs = 10
    warmup_epochs = 2
    model = SimCLRInnerEye(encoder_name="resnet18", dataset_name="CIFAR10",
                           gpus=gpus, num_samples=num_samples, batch_size=batch_size,
                           max_epochs=max_epochs, warmup_epochs=warmup_epochs)
    # The LR scheduler used here works per step. Scheduler computes the total number of steps, in this example that's 5
    train_iters_per_epoch = num_samples / (batch_size * gpus)
    assert model.train_iters_per_epoch == train_iters_per_epoch
    # Mock a second optimizer that is normally created in the SSL container
    linear_head_optimizer = mock.MagicMock()
    model.online_eval_optimizer = linear_head_optimizer
    # Retrieve the scheduler and iterate it
    _, scheduler_list = model.configure_optimizers()
    assert isinstance(scheduler_list[0], dict)
    assert scheduler_list[0]["interval"] == "step"
    scheduler = scheduler_list[0]["scheduler"]
    assert isinstance(scheduler, _LRScheduler)
    lr = []
    for i in range(0, int(max_epochs * train_iters_per_epoch)):
        scheduler.step()
        lr.append(scheduler.get_last_lr()[0])
    # The highest learning rate is expected after the warmup epochs
    highest_lr = np.argmax(lr)
    assert highest_lr == int(warmup_epochs * train_iters_per_epoch - 1)

    for i in range(0, highest_lr):
        assert lr[i] < lr[i + 1], f"Not strictly monotonically increasing at index {i}"
    for i in range(highest_lr, len(lr) - 1):
        assert lr[i] > lr[i + 1], f"Not strictly monotonically decreasing at index {i}"


def test_online_evaluator_recovery(test_output_dirs: OutputFolderForTests) -> None:
    """
    Test checkpoint recovery for the online evaluator in an end-to-end training run.
    """
    container = DummyContainerWithModel()
    model = container.create_model()
    data = container.get_data_module()
    checkpoint_folder = test_output_dirs.create_file_or_folder_path("checkpoints")
    checkpoint_folder.mkdir(exist_ok=True)
    checkpoints = ModelCheckpoint(dirpath=checkpoint_folder,
                                  every_n_val_epochs=1,
                                  save_last=True)
    # Create a first callback, that will be used in training.
    callback1 = SSLOnlineEvaluatorInnerEye(class_weights=None,
                                           z_dim=1,
                                           num_classes=2,
                                           dataset="foo",
                                           drop_p=0.2,
                                           learning_rate=1e-5)
    # To simplify the test setup, do not run any actual training (this would require complicated dataset with a
    # combined loader)
    with mock.patch(
            "InnerEye.ML.SSL.lightning_modules.ssl_online_evaluator.SSLOnlineEvaluatorInnerEye.on_train_batch_end",
            return_value=None) as mock_train:
        with mock.patch(
                "InnerEye.ML.SSL.lightning_modules.ssl_online_evaluator.SSLOnlineEvaluatorInnerEye"
                ".on_validation_batch_end",
                return_value=None):
            trainer = Trainer(default_root_dir=str(test_output_dirs.root_dir),
                              callbacks=[checkpoints, callback1],
                              max_epochs=10)
            trainer.fit(model, datamodule=data)
            # Check that the callback was actually used
            mock_train.assert_called()
            # Now read out the parameters of the callback.
            # We will then run a second training job, with a new callback object, that will be initialized randomly,
            # and should have different parameters initially. After checkpoint recovery, it should have exactly the
            # same parameters as the first callback.
            parameters1 = list(callback1.evaluator.parameters())
            callback2 = SSLOnlineEvaluatorInnerEye(class_weights=None,
                                                   z_dim=1,
                                                   num_classes=2,
                                                   dataset="foo",
                                                   drop_p=0.2,
                                                   learning_rate=1e-5)
            # Ensure that the parameters are really different initially
            parameters2_before_training = list(callback2.evaluator.parameters())
            assert not torch.allclose(parameters2_before_training[0], parameters1[0])
            # Start a second training run with recovery
            last_checkpoint = checkpoints.last_model_path
            trainer2 = Trainer(default_root_dir=str(test_output_dirs.root_dir),
                               callbacks=[callback2],
                               max_epochs=20,
                               resume_from_checkpoint=last_checkpoint)
            trainer2.fit(model, datamodule=data)
            # Read the parameters and check if they are the same as what was stored in the first callback.
            parameters2_after_training = list(callback2.evaluator.parameters())
            assert torch.allclose(parameters2_after_training[0], parameters1[0])

    # It's somewhat obsolete, but we can now check that the checkpoint file really contained the optimizer and weights
    checkpoint = torch.load(last_checkpoint)
    assert "callbacks" in checkpoint
    callback_name = SSLOnlineEvaluatorInnerEye.__name__
    assert callback_name in checkpoint["callbacks"]
    callback_state = checkpoint["callbacks"][callback_name]
    assert SSLOnlineEvaluatorInnerEye.OPTIMIZER_STATE_NAME in callback_state
    assert SSLOnlineEvaluatorInnerEye.EVALUATOR_STATE_NAME in callback_state


@pytest.mark.gpu
def test_online_evaluator_not_distributed() -> None:
    """
    Check if the online evaluator uses the DDP flag correctly when running not distributed
    """
    with mock.patch("InnerEye.ML.SSL.lightning_modules.ssl_online_evaluator.DistributedDataParallel") as mock_ddp:
        callback = SSLOnlineEvaluatorInnerEye(class_weights=None,
                                              z_dim=1,
                                              num_classes=2,
                                              dataset="foo",
                                              drop_p=0.2,
                                              learning_rate=1e-5)
        mock_ddp.assert_not_called()

        # Standard trainer without DDP
        trainer = Trainer()
        # Test the flag that the internal logic of on_pretrain_routine_start uses
        assert hasattr(trainer, "_accelerator_connector")
        assert not trainer._accelerator_connector.is_distributed
        mock_module = mock.MagicMock(device=torch.device("cpu"))
        callback.on_pretrain_routine_start(trainer, mock_module)
        assert isinstance(callback.evaluator, Module)
        mock_ddp.assert_not_called()


@pytest.mark.gpu
def test_online_evaluator_distributed() -> None:
    """
    Check if the online evaluator uses the DDP flag correctly when running distributed.
    """
    mock_ddp_result = "mock_ddp_result"
    mock_sync_result = "mock_sync_result"
    with mock.patch("InnerEye.ML.SSL.lightning_modules.ssl_online_evaluator.SyncBatchNorm.convert_sync_batchnorm",
                    return_value=mock_sync_result) as mock_sync:
        with mock.patch("InnerEye.ML.SSL.lightning_modules.ssl_online_evaluator.DistributedDataParallel",
                        return_value=mock_ddp_result) as mock_ddp:
            callback = SSLOnlineEvaluatorInnerEye(class_weights=None,
                                                  z_dim=1,
                                                  num_classes=2,
                                                  dataset="foo",
                                                  drop_p=0.2,
                                                  learning_rate=1e-5)

            # Trainer with DDP
            device = torch.device("cuda:0")
            mock_module = mock.MagicMock(device=device)
            trainer = Trainer(accelerator="ddp", gpus=2)
            # Test the two flags that the internal logic of on_pretrain_routine_start uses
            assert trainer._accelerator_connector.is_distributed
            assert trainer._accelerator_connector.use_ddp
            original_evaluator = callback.evaluator
            callback.on_pretrain_routine_start(trainer, mock_module)
            # Check that SyncBatchNorm has been turned on
            mock_sync.assert_called_once_with(original_evaluator)
            # Check that the evaluator has been turned into a DDP object
            # We still need to mock DDP here because the constructor relies on having a process group available
            mock_ddp.assert_called_once_with(mock_sync_result, device_ids=[device])
            assert callback.evaluator == mock_ddp_result
