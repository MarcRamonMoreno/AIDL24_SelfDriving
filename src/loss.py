from torch.nn.modules.loss import _Loss
from torch.autograd import Variable
import torch
import torch.nn as nn
from torch.functional import F

DEVICE = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


class diceloss(torch.nn.Module):
    def init(self):
        super(diceLoss, self).init()

    def forward(self, pred, target):
        smooth = 1.0
        iflat = pred.contiguous().view(-1)
        tflat = target.contiguous().view(-1)
        intersection = (iflat * tflat).sum()
        A_sum = torch.sum(iflat * iflat)
        B_sum = torch.sum(tflat * tflat)
        return 1 - ((2.0 * intersection + smooth) / (A_sum + B_sum + smooth))


"""
This is the implementation of following paper:
https://arxiv.org/pdf/1802.05591.pdf
"""


class FocalLoss(nn.Module):
    """
    Binary Focal Loss implementation.
    """

    def __init__(
        self,
        gamma=2,
        alpha=0.25,
        reduction="mean",
        device="cuda" if torch.cuda.is_available() else "cpu",
    ):
        super().__init__()
        self.gamma = gamma
        self.alpha = alpha
        self.reduction = reduction
        self.device = device

    def forward(self, input, target):
        # Debugging statement to ensure alpha is a scalar
        # Apply sigmoid to get the probabilities
        pt = torch.sigmoid(input)
        pt = pt.clamp(min=0.000001, max=0.999999)

        # Convert target to float
        target = target.float()

        # Calculate the focal loss
        loss = -self.alpha * (1 - pt) ** self.gamma * target * torch.log(pt) - (
            1 - self.alpha
        ) * pt**self.gamma * (1 - target) * torch.log(1 - pt)

        if self.reduction == "mean":
            loss = torch.mean(loss)
        elif self.reduction == "sum":
            loss = torch.sum(loss)

        return loss


class DiscriminativeLoss(_Loss):
    def __init__(
        self,
        delta_var=0.5,
        delta_dist=1.5,
        norm=2,
        alpha=1.0,
        beta=1.0,
        gamma=0.001,
        usegpu=False,
        size_average=True,
    ):
        super(DiscriminativeLoss, self).__init__(reduction="mean")
        self.delta_var = delta_var
        self.delta_dist = delta_dist
        self.norm = norm
        self.alpha = alpha
        self.beta = beta
        self.gamma = gamma
        self.usegpu = usegpu
        assert self.norm in [1, 2]

    def forward(self, input, target):

        return self._discriminative_loss(input, target)

    def _discriminative_loss(self, embedding, seg_gt):
        batch_size, embed_dim, H, W = embedding.shape
        embedding = embedding.reshape(batch_size, embed_dim, H * W)
        seg_gt = seg_gt.reshape(batch_size, H * W)

        var_loss = torch.tensor(0, dtype=embedding.dtype, device=embedding.device)
        dist_loss = torch.tensor(0, dtype=embedding.dtype, device=embedding.device)
        reg_loss = torch.tensor(0, dtype=embedding.dtype, device=embedding.device)

        for b in range(batch_size):
            embedding_b = embedding[b]  # (embed_dim, H*W)
            seg_gt_b = seg_gt[b]  # (H*W)

            labels, indexs = torch.unique(seg_gt_b, return_inverse=True)
            num_lanes = len(labels)
            if num_lanes == 0:
                _nonsense = embedding.sum()
                _zero = torch.zeros_like(_nonsense)
                var_loss = var_loss + _nonsense * _zero
                dist_loss = dist_loss + _nonsense * _zero
                reg_loss = reg_loss + _nonsense * _zero
                continue

            centroid_mean = []
            for lane_idx in labels:
                seg_mask_i = seg_gt_b == lane_idx

                if not seg_mask_i.any():
                    continue

                embedding_i = embedding_b * seg_mask_i
                mean_i = torch.sum(embedding_i, dim=1) / torch.sum(seg_mask_i)
                centroid_mean.append(mean_i)
                # ---------- var_loss -------------
                var_loss = (
                    var_loss
                    + torch.sum(
                        F.relu(
                            torch.norm(
                                embedding_i[:, seg_mask_i]
                                - mean_i.reshape(embed_dim, 1),
                                dim=0,
                            )
                            - self.delta_var
                        )
                        ** 2
                    )
                    / torch.sum(seg_mask_i)
                    / num_lanes
                )
            centroid_mean = torch.stack(centroid_mean)  # (n_lane, embed_dim)

            if num_lanes > 1:
                centroid_mean1 = centroid_mean.reshape(-1, 1, embed_dim)
                centroid_mean2 = centroid_mean.reshape(1, -1, embed_dim)

                dist = torch.norm(
                    centroid_mean1 - centroid_mean2, dim=2
                )  # shape (num_lanes, num_lanes)
                dist = (
                    dist
                    + torch.eye(num_lanes, dtype=dist.dtype, device=dist.device)
                    * self.delta_dist
                )

                # divided by two for double calculated loss above, for implementation convenience
                dist_loss = (
                    dist_loss
                    + torch.sum(F.relu(-dist + self.delta_dist) ** 2)
                    / (num_lanes * (num_lanes - 1))
                    / 2
                )

            # reg_loss is not used in original paper
            # reg_loss = reg_loss + torch.mean(torch.norm(centroid_mean, dim=1))

        var_loss = var_loss / batch_size
        dist_loss = dist_loss / batch_size
        reg_loss = reg_loss / batch_size

        return var_loss, dist_loss, reg_loss