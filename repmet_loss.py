import torch
import torch.nn.functional as F
import torch.nn as nn

from loss import Loss
from utils import ensure_tensor

class RepMetLoss(Loss):

    def __init__(self, set_y, k, m, d, measure='euclidean', alpha=1.0):
        super().__init__(set_y, k, m, d, measure, alpha)

    def update_clusters(self, set_x, max_iter=20):
        """
        Given an array of representations for the entire training set,
        recompute clusters and store example cluster assignments in a
        quickly sampleable form.
        """
        super().update_clusters(set_x, max_iter=max_iter)

        # make leaf variable after editing it then wrap in param
        self.centroids = nn.Parameter(torch.cuda.FloatTensor(self.centroids).requires_grad_())

    def loss(self, x, y):
        """Compute repmet loss.

        Given a tensor of features `x`, the assigned class for each example,
        compute the repmet loss according to equations (4) and (5) in
        https://arxiv.org/pdf/1806.04728.pdf.

        Args:
            x: A batch of features.
            y: Class labels for each example.

        Returns:
            total_loss: The total magnet loss for the batch.
            losses: The loss for each example in the batch.
            acc: The predictive accuracy of the batch
        """

        # Compute distance of each example to each cluster centroid (euclid without the root)
        sample_costs = self.calculate_distance(self.centroids, x)

        # Compute the two masks selecting the sample_costs related to each class(r)=class(cluster/rep)
        intra_cluster_mask = self.comparison_mask(y, torch.from_numpy(self.cluster_classes).cuda())
        intra_cluster_mask_op = ~intra_cluster_mask

        # Calculate the minimum distance to rep of different class
        # therefore we make all values for correct clusters bigger so they are ignored..
        intra_cluster_costs_ignore_op = (intra_cluster_mask.float() * (sample_costs.max() * 1.5))
        intra_cluster_costs_desired_op = intra_cluster_mask_op.float() * sample_costs
        intra_cluster_costs_together_op = intra_cluster_costs_ignore_op + intra_cluster_costs_desired_op
        min_non_match, _ = intra_cluster_costs_together_op.min(1)

        # Calculate the minimum distance to rep of same class
        # therefore we make all values for other clusters bigger so they are ignored..
        intra_cluster_costs_ignore = (intra_cluster_mask_op.float() * (sample_costs.max() * 1.5))
        intra_cluster_costs_desired = intra_cluster_mask.float() * sample_costs
        intra_cluster_costs_together = intra_cluster_costs_ignore + intra_cluster_costs_desired
        min_match, _ = intra_cluster_costs_together.min(1)

        # Compute variance of intra-cluster distances
        # N = x.shape[0]
        # variance = min_match.sum() / float((N - 1))
        variance = 0.5  # hard code 0.5 [as suggested in paper] but seems to now work as well as the calculated variance in my exp
        var_normalizer = -1 / (2 * variance)

        if not self.avg_variance:
            self.avg_variance = variance
        else:
            self.avg_variance = (self.avg_variance + variance) / 2

        # Compute numerator
        numerator = torch.exp(var_normalizer * min_match)

        # Compute denominator
        # diff_class_mask = tf.to_float(tf.logical_not(comparison_mask(classes, cluster_classes)))
        diff_class_mask = intra_cluster_mask_op.float()
        denom_sample_costs = torch.exp(var_normalizer * sample_costs)
        denominator = (diff_class_mask.cuda() * denom_sample_costs).sum(1)

        # Compute example losses and total loss
        epsilon = 1e-8
        losses_eq4 = F.relu(min_match - min_non_match + self.alpha)
        # total_loss_eq4 = losses_eq4.mean()

        # Compute example losses and total loss
        losses_eq5 = F.relu(-torch.log(numerator / (denominator + epsilon) + epsilon) + self.alpha)
        # total_loss_eq5 = losses_eq5.mean()

        losses = losses_eq5#losses_eq4 + losses_eq5
        total_loss = losses.mean()

        _, preds = sample_costs.min(1)
        preds = ensure_tensor(self.cluster_classes[preds]).cuda()  # convert from cluster ids to class ids
        acc = torch.eq(y, preds).float().mean()

        return total_loss, losses, acc



