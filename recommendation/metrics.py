from typing import Sequence

import torch
import torchbearer
from torchbearer import metrics, Metric
from torchbearer.metrics import default_for_key, running_mean, mean


@metrics.default_for_key("binary_accuracy")
@running_mean
@mean
@metrics.lambda_metric("binary_accuracy", on_epoch=False)
def binary_accuracy(y_pred: torch.Tensor, y_true: torch.Tensor, threshold: float = 0.5):
    if isinstance(y_true, Sequence) and isinstance(y_pred, torch.Tensor):
        y_true = y_true[0]
    if y_true.layout == torch.sparse_coo:
        y_true = y_true.to_dense()

    y_pred = (y_pred.float() > threshold).long()
    y_true = (y_true.float() > threshold).long()

    return torch.eq(y_pred, y_true).view(-1).float()


@metrics.default_for_key("precision")
@running_mean
@mean
@metrics.lambda_metric("precision", on_epoch=False)
def precision(y_pred: torch.Tensor, y_true: torch.Tensor, threshold: float = 0.5, eps=1e-9):
    if isinstance(y_true, Sequence) and isinstance(y_pred, torch.Tensor):
        y_true = y_true[0]
    if y_true.layout == torch.sparse_coo:
        y_true = y_true.to_dense()
    y_pred = (y_pred.float() > threshold).float()
    y_true = (y_true.float() > threshold).float()

    true_positive = (y_pred * y_true).sum(dim=-1)
    return true_positive.div(y_pred.sum(dim=-1).add(eps))


@metrics.default_for_key("recall")
@running_mean
@mean
@metrics.lambda_metric("recall", on_epoch=False)
def recall(y_pred: torch.Tensor, y_true: torch.Tensor, threshold: float = 0.5, eps=1e-9):
    if isinstance(y_true, Sequence) and isinstance(y_pred, torch.Tensor):
        y_true = y_true[0]
    if y_true.layout == torch.sparse_coo:
        y_true = y_true.to_dense()
    y_pred = (y_pred.float() > threshold).float()
    y_true = (y_true.float() > threshold).float()

    true_positive = (y_pred * y_true).sum(dim=-1)
    return true_positive.div(y_true.sum(dim=-1).add(eps))


@default_for_key('masked_zeroes_mse')
@running_mean
@mean
class MaskedZeroesMeanSquaredError(Metric):
    """Masked Zeroes Mean squared error metric. Computes the pixelwise squared error which is then averaged with decorators.
    Decorated with a mean and running_mean. Default for key: 'masked_zeroes_mse'.

    Args:
        pred_key (StateKey): The key in state which holds the predicted values
        target_key (StateKey): The key in state which holds the target values
    """

    def __init__(self, pred_key=torchbearer.Y_PRED, target_key=torchbearer.Y_TRUE):
        super().__init__('masked_zeroes_mse')
        self.pred_key = pred_key
        self.target_key = target_key

    def process(self, *args):
        state = args[0]
        y_pred = state[self.pred_key]
        y_true = state[self.target_key]
        if y_true.layout == torch.sparse_coo:
            y_true = y_true.to_dense()

        mask = y_true.ne(0)
        y_pred = y_pred.masked_select(mask)
        y_true = y_true.masked_select(mask)

        return torch.pow(y_pred - y_true.view_as(y_pred), 2).data
