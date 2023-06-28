# Copyright (c) OpenMMLab. All rights reserved.
import json
import warnings

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from ..builder import LOSSES


def prepare_tensors(pred, label, ignore_index, ancestors, descendants,
                    anc_indexes):

    num_classes = pred.shape[1]
    label = label.flatten()
    valid_mask = label != ignore_index
    label = label[valid_mask]

    multi_hot = torch.empty(label.shape[0], num_classes).to(pred.device)
    for cl in torch.unique(label):
        multi_hot[label == cl] = torch.zeros(num_classes).scatter_(
            0, torch.tensor(ancestors[str(cl.item())]),
            1.).to(multi_hot.device)

    pred = pred.transpose(1, 3).transpose(1,
                                          2).reshape(-1,
                                                     num_classes)[valid_mask]
    new_pred = pred.clone()
    new_pred[multi_hot.bool()] = torch.min(
        torch.stack([
            torch.gather(pred, 1,
                         i.to(new_pred.device).repeat(new_pred.shape[0], 1))
            for i in anc_indexes
        ]),
        dim=0)[0][multi_hot.bool()]

    for cl in range(41, num_classes):
        new_pred[:, cl][torch.logical_not(multi_hot)[:, cl]] = torch.max(
            torch.stack([pred[:, d] for d in descendants[str(cl)]]),
            dim=0)[0][torch.logical_not(multi_hot)[:, cl]]
    return new_pred, multi_hot


def focal_tree_min_loss(pred,
                        label,
                        gamma,
                        weight=None,
                        class_weight=None,
                        reduction='mean',
                        avg_factor=None,
                        ignore_index=-100,
                        avg_non_ignore=False,
                        ancestors=None,
                        descendants=None,
                        anc_indexes=None):
    """cross_entropy. The wrapper function for :func:`F.cross_entropy`

    Args:
        pred (torch.Tensor): The prediction with shape (N, 1).
        label (torch.Tensor): The learning label of the prediction.
        weight (torch.Tensor, optional): Sample-wise loss weight.
            Default: None.
        class_weight (list[float], optional): The weight for each class.
            Default: None.
        reduction (str, optional): The method used to reduce the loss.
            Options are 'none', 'mean' and 'sum'. Default: 'mean'.
        avg_factor (int, optional): Average factor that is used to average
            the loss. Default: None.
        ignore_index (int): Specifies a target value that is ignored and
            does not contribute to the input gradients. When
            ``avg_non_ignore `` is ``True``, and the ``reduction`` is
            ``''mean''``, the loss is averaged over non-ignored targets.
            Defaults: -100.
        avg_non_ignore (bool): The flag decides to whether the loss is
            only averaged over non-ignored targets. Default: False.
            `New in version 0.23.0.`
    """

    if class_weight is not None:
        class_weight = pred.new_tensor(class_weight)
    else:
        class_weight = None

    # Create hierarchical multi-hot vectors
    pred, target = prepare_tensors(pred, label, ignore_index, ancestors,
                                   descendants, anc_indexes)

    pred_sigmoid = pred.sigmoid()
    target = target.type_as(pred)
    one_minus_pt = (1 - pred_sigmoid) * target + pred_sigmoid * (1 - target)
    focal_weight = one_minus_pt.pow(gamma)

    # class_weight is a manual rescaling weight given to each class.
    # If given, has to be a Tensor of size C element-wise losses
    loss = F.binary_cross_entropy_with_logits(
        pred, target, reduction='none') * focal_weight

    if reduction == 'mean':
        loss = loss.mean()
    elif reduction == 'sum':
        loss = loss.sum()

    return loss


@LOSSES.register_module()
class FocalTreeMinLoss(nn.Module):
    """TreeMinLoss.

    Args:
        use_sigmoid (bool, optional): Whether the prediction uses sigmoid
            of softmax. Defaults to False.
        use_mask (bool, optional): Whether to use mask cross entropy loss.
            Defaults to False.
        reduction (str, optional): . Defaults to 'mean'.
            Options are "none", "mean" and "sum".
        class_weight (list[float] | str, optional): Weight of each class. If in
            str format, read them from a file. Defaults to None.
        loss_weight (float, optional): Weight of the loss. Defaults to 1.0.
        loss_name (str, optional): Name of the loss item. If you want this loss
            item to be included into the backward graph, `loss_` must be the
            prefix of the name. Defaults to 'loss_ce'.
        avg_non_ignore (bool): The flag decides to whether the loss is
            only averaged over non-ignored targets. Default: False.
            `New in version 0.23.0.`s
    """

    def __init__(self,
                 gamma=2.0,
                 use_sigmoid=True,
                 reduction='mean',
                 class_weight=None,
                 loss_weight=1.0,
                 loss_name='tree_min_loss',
                 avg_non_ignore=False,
                 ancestors_path=None,
                 descendants_path=None,
                 class_weight_path=None,
                 ancestors_indexes_path=None):
        super(FocalTreeMinLoss, self).__init__()
        assert ancestors_path
        assert descendants_path
        self.gamma = gamma
        self.reduction = reduction
        self.loss_weight = loss_weight
        # self.class_weight = get_class_weight(class_weight)
        with open(class_weight_path) as class_weight_file:
            class_weight = json.load(class_weight_file)
            self.class_weight = np.concatenate(
                (class_weight['third_level'], class_weight['second_level'],
                 class_weight['first_level']))
        self.avg_non_ignore = avg_non_ignore
        if not self.avg_non_ignore and self.reduction == 'mean':
            warnings.warn(
                'Default ``avg_non_ignore`` is False, if you would like to '
                'ignore the certain label and average loss over non-ignore '
                'labels, which is the same with PyTorch official '
                'cross_entropy, set ``avg_non_ignore=True``.')

        with open(ancestors_path) as ancestors_file:
            self.ancestors = json.load(ancestors_file)
        with open(descendants_path) as descendants_file:
            self.descendants = json.load(descendants_file)
        self._loss_name = loss_name
        self.ancestors_indexes = torch.from_numpy(
            np.load(ancestors_indexes_path))

    def extra_repr(self):
        """Extra repr."""
        s = f'avg_non_ignore={self.avg_non_ignore}'
        return s

    def forward(self,
                cls_score,
                label,
                weight=None,
                avg_factor=None,
                reduction_override=None,
                ignore_index=-100,
                **kwargs):
        """Forward function."""
        assert reduction_override in (None, 'none', 'mean', 'sum')
        reduction = (
            reduction_override if reduction_override else self.reduction)
        # if self.class_weight is not None:
        #     class_weight = cls_score.new_tensor(self.class_weight)
        # else:
        #     class_weight = None
        # Note: for BCE loss, label < 0 is invalid.

        loss = focal_tree_min_loss(
            cls_score,
            label,
            self.gamma,
            weight,
            class_weight=self.class_weight,
            reduction=reduction,
            avg_factor=avg_factor,
            avg_non_ignore=self.avg_non_ignore,
            ignore_index=ignore_index,
            ancestors=self.ancestors,
            descendants=self.descendants,
            anc_indexes=self.ancestors_indexes)

        loss_cls = self.loss_weight * loss
        return loss_cls

    @property
    def loss_name(self):
        """Loss Name.

        This function must be implemented and will return the name of this
        loss function. This name will be used to combine different loss items
        by simple sum operation. In addition, if you want this loss item to be
        included into the backward graph, `loss_` must be the prefix of the
        name.

        Returns:
            str: The name of this loss item.
        """
        return self._loss_name
