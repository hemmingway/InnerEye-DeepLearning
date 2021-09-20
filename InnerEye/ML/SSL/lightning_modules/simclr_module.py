#  ------------------------------------------------------------------------------------------
#  Copyright (c) Microsoft Corporation. All rights reserved.
#  Licensed under the MIT License (MIT). See LICENSE in the repo root for license information.
#  ------------------------------------------------------------------------------------------

from typing import Any, Dict, List, Optional, Tuple, Union

import torch
import torch.nn as nn
import torch.nn.functional as F
from pl_bolts.models.self_supervised.simclr.simclr_module import SimCLR
from torch import Tensor as T
from torch.optim import Optimizer
from torch.optim.lr_scheduler import StepLR, _LRScheduler

from InnerEye.ML.SSL.encoders import SSLEncoder
from InnerEye.ML.SSL.utils import SSLDataModuleType

SingleBatchType = Tuple[List, T]
BatchType = Union[Dict[SSLDataModuleType, SingleBatchType], SingleBatchType]


class _Projection(nn.Module):
    def __init__(self, input_dim: int, hidden_dim: int, output_dim: int) -> None:
        super().__init__()
        self.output_dim = output_dim
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim

        self.model = nn.Sequential(
            nn.Linear(self.input_dim, self.hidden_dim, bias=True),
            nn.BatchNorm1d(self.hidden_dim),
            nn.ReLU(),
            nn.Linear(self.hidden_dim, self.output_dim, bias=False))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.model(x)
        return F.normalize(x, dim=1)


class SimCLRInnerEye(SimCLR):
    def __init__(self, encoder_name: str, dataset_name: str, use_7x7_first_conv_in_resnet: bool = True,
                 **kwargs: Any) -> None:
        """
        Returns SimCLR pytorch-lightning module, based on lightning-bolts implementation.
        :param encoder_name: Image encoder name (predefined models)
        :param dataset_name: Dataset name (e.g. cifar10, kaggle, etc.)
        :param use_7x7_first_conv_in_resnet: If True, use a 7x7 kernel (default) in the first layer of resnet.
            If False, replace first layer by a 3x3 kernel. This is required for small CIFAR 32x32 images to not
            shrink them.
        """
        if "dataset" not in kwargs:  # needed for the new version of lightning-bolts
            kwargs.update({"dataset": dataset_name})
        super().__init__(**kwargs)
        self.save_hyperparameters()
        self.encoder = SSLEncoder(encoder_name, use_7x7_first_conv_in_resnet)
        self.projection = _Projection(input_dim=self.encoder.get_output_feature_dim(), hidden_dim=2048, output_dim=128)
        # The optimizer for the linear head is managed by this module, so that its state can be stored in a checkpoint
        # automatically by Lightning. The training for the linear head is outside of this module though.
        self.automatic_optimization = False

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.encoder(x)

    def training_step(self, batch: BatchType, batch_idx: int, optimizer_idx: int) -> T:
        if optimizer_idx != 0:
            return
        optimizers = self.optimizers()
        assert len(optimizers) == 2, "Expected to get 2 optimizers, one for the embedding and one for the linear head"
        loss = self.shared_step(batch)
        optimizers[0].zero_grad()
        self.manual_backward(loss)
        optimizers[0].step()

    def shared_step(self, batch: BatchType) -> T:
        batch = batch[SSLDataModuleType.ENCODER] if isinstance(batch, dict) else batch

        (img1, img2), y = batch

        # get h representations, bolts resnet returns a list
        h1, h2 = self(img1), self(img2)

        # get z representations
        z1 = self.projection(h1)
        z2 = self.projection(h2)

        loss = self.nt_xent_loss(z1, z2, self.temperature)

        return loss

    def configure_optimizers(self) -> Tuple[List[Optimizer], List[_LRScheduler]]:
        base_optim, base_scheduler = super().configure_optimizers()
        base_optim.append(self.online_eval_optimizer)
        # When returning two optimizers, also create a second scheduler. Make that effectively use constant LR.
        base_scheduler.append(StepLR(self.online_eval_optimizer, step_size=1, gamma=1.0))
        return base_optim, base_scheduler




