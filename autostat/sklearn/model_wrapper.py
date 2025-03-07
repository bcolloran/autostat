import typing as ty

import numpy as np
from numpy.typing import ArrayLike, NDArray
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import (
    Kernel,
    Sum,
)

from .to_kernel_spec import to_kernel_spec

from ..kernel_specs import (
    TopLevelKernelSpec,
    KernelSpec,
)
from ..run_settings import KernelSearchSettings
from ..dataset_adapters import Dataset, ModelPredictions
from .kernel_builder import build_kernel
from ..math import calc_bic
from ..compositional_gp_model import CompositionalGPModel


class SklearnCompositionalGPModel(CompositionalGPModel):
    def __init__(
        self,
        kernel_spec: KernelSpec,
        data: Dataset,
        run_settings: KernelSearchSettings,
        alpha=1e-7,
    ) -> None:
        self.kernel_spec = kernel_spec
        self.data = data
        self.run_settings = run_settings

        kernel = build_kernel(kernel_spec)

        self.gp = GaussianProcessRegressor(
            kernel=kernel,
            alpha=alpha,
            normalize_y=False,
            n_restarts_optimizer=run_settings.sklean_n_restarts_optimizer,
        )
        self.training_predictions: ty.Union[ModelPredictions, None] = None
        self.test_predictions: ty.Union[ModelPredictions, None] = None

    def fit(self) -> None:
        self.gp.fit(self.data.train_x, self.data.train_y)

    def log_likelihood(self) -> float:
        k = ty.cast(Kernel, self.gp.kernel_)
        ll: float = ty.cast(float, self.gp.log_marginal_likelihood(k.theta))
        return ll

    def bic(self) -> float:
        return calc_bic(
            self.kernel_spec.num_params(),
            self.data.train_x.shape[0],
            self.log_likelihood(),
        )

    def predict(self, x):
        return self._predict(x)

    def predict_train(self):
        return self._predict_cached(train=True)

    def predict_test(self):
        return self._predict_cached(train=False)

    def _predict_cached(self, train=True) -> ModelPredictions:
        if train:
            if self.training_predictions is None:
                self.training_predictions = self._predict(self.data.train_x)
            return self.training_predictions
        elif self.data.test_x is not None:
            if self.test_predictions is None:
                self.test_predictions = self._predict(self.data.test_x)
            return self.test_predictions
        else:
            raise ValueError(
                "cannot `_predict_cached(train=True)` if `self.data.test_x is None`"
            )

    def _predict(self, x: ArrayLike) -> ModelPredictions:
        y_pred, cov = ty.cast(
            tuple[NDArray[np.float_], NDArray[np.float_]],
            self.gp.predict(x, return_std=False, return_cov=True),
        )
        y_pred = y_pred.flatten()
        y_std = np.sqrt(np.diag(cov)).flatten()

        return ModelPredictions(y_pred, y_std, cov)

    def residuals(self) -> NDArray[np.float_]:
        yHat, _, _ = self.predict_train()
        yHat = yHat.flatten()
        train_y = self.data.train_y.flatten()
        residuals = train_y - yHat
        return residuals

    def to_spec(self) -> TopLevelKernelSpec:
        return to_kernel_spec(ty.cast(Sum, self.gp.kernel_))

    def log_likelihood_test(self) -> float:
        y_pred, _, cov = self.predict_test()
        if self.data.test_y is None:
            raise ValueError(
                "cannot get log_likelihood_test if self.data.test_y is None"
            )
        y_test = self.data.test_y

        Y = y_pred.reshape((-1, 1)) - y_test.reshape((-1, 1))
        N = len(y_pred)

        L = np.linalg.cholesky(cov)
        L_inv = np.linalg.inv(np.linalg.cholesky(cov))
        Sigma_inv = L_inv.T @ L_inv
        _, log_det_L = np.linalg.slogdet(L)
        log_det_K = 2 * log_det_L

        log_prob_score = (
            -0.5 * (Y.T @ Sigma_inv @ Y + log_det_K + N * np.log(2 * np.pi)).item()
        )
        return log_prob_score

    def prediction_log_prob_score(self) -> float:
        # NOTE: this is kind of a variance weighted L2 squared error metric.
        # We assume that
        # y_test_t = y_pred_t + ε_t , with ε_t ~ N(0, σ_t^2 * I)
        # -- where the epsilons
        # ε_t = y_test_t - y_pred_t
        # are not identically distributed, but are assumed to be independent.
        # Under this assumption, the score does not depend on the covariance
        # structure of the kernel matrix.

        if self.data.test_y is None:
            raise ValueError("prediction_log_prob_score requires test data")

        y_pred, y_std, _ = self.predict_test()
        y_test = self.data.test_y

        z_score_sqr = ((y_pred - y_test) / y_std) ** 2
        N = len(y_pred)

        log_prob_score = -0.5 * N * np.log(2 * np.pi) - np.sum(
            0.5 * z_score_sqr + np.log(y_std)
        )
        return log_prob_score
