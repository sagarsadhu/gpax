from functools import partial
from typing import Callable, Dict, Optional, Tuple

import jax
import jax.numpy as jnp
import numpyro
import numpyro.distributions as dist
from numpyro.infer import SVI, Trace_ELBO
from numpyro.infer.autoguide import AutoDelta
from jax import jit
from jax.interpreters import xla
import haiku as hk

from .gp import ExactGP
from .kernels import get_kernel


class viDKL(ExactGP):
    """
    Deep kernel learning with determenistic NN
    and variational inference of GP hyperprameters

    Args:
        input_dim: number of input dimensions
        z_dim: latent space dimensionality
        kernel: type of kernel ('RBF', 'Matern', 'Periodic')
        kernel_prior: optional priors over kernel hyperparameters (uses LogNormal(0,1) by default)
        nn: Custom neural network
        latent_prior: Optional prior over the latent space (BNN embedding)
    """

    def __init__(self, input_dim: int, z_dim: int = 2, kernel: str = 'RBF',
                 kernel_prior: Optional[Callable[[], Dict[str, jnp.ndarray]]] = None,
                 nn: Optional[Callable[jnp.ndarray, jnp.ndarray]] = None,
                 latent_prior: Optional[Callable[[jnp.ndarray], Dict[str, jnp.ndarray]]] = None
                 ) -> None:
        super(viDKL, self).__init__(input_dim, kernel, kernel_prior)
        xla._xla_callable.cache_clear()
        self.feature_extractor = nn if nn else mlp
        self.kernel_dim = z_dim
        self.latent_prior = latent_prior

    def model(self, X: jnp.ndarray, y: jnp.ndarray) -> None:
        """DKL probabilistic model"""
        # NN part
        z = self.feature_extractor(X)
        if self.latent_prior:  # Sample latent variable
            z = self.latent_prior(z)
        # Sample GP kernel parameters
        if self.kernel_prior:
            kernel_params = self.kernel_prior()
        else:
            kernel_params = self._sample_kernel_params()
        # Sample noise
        noise = numpyro.sample("noise", dist.LogNormal(0.0, 1.0))
        # GP's mean function
        f_loc = jnp.zeros(z.shape[0])
        # compute kernel
        k = get_kernel(self.kernel)(
            z, z,
            kernel_params,
            noise
        )
        # sample y according to the standard Gaussian process formula
        numpyro.sample(
            "y",
            dist.MultivariateNormal(loc=f_loc, covariance_matrix=k),
            obs=y,
        )

    @partial(jit, static_argnames='self')
    def get_mvn_posterior(self,
                          X_test: jnp.ndarray, params: Dict[str, jnp.ndarray]
                          ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        """
        Returns parameters (mean and cov) of multivariate normal posterior
        for a single sample of DKL hyperparameters
        """
        noise = params["noise"]
        # embed data intot the latent space
        z_train = self.feature_extractor(self.X_train)
        z_test = self.feature_extractor(X_test)
        # compute kernel matrices for train and test data
        k_pp = get_kernel(self.kernel)(z_test, z_test, params, noise)
        k_pX = get_kernel(self.kernel)(z_test, z_train, params, jitter=0.0)
        k_XX = get_kernel(self.kernel)(z_train, z_train, params, noise)
        # compute the predictive covariance and mean
        K_xx_inv = jnp.linalg.inv(k_XX)
        cov = k_pp - jnp.matmul(k_pX, jnp.matmul(K_xx_inv, jnp.transpose(k_pX)))
        mean = jnp.matmul(k_pX, jnp.matmul(K_xx_inv, self.y_train))
        return mean, cov

    def fit(self, rng_key: jnp.array, X: jnp.ndarray, y: jnp.ndarray,
            num_steps: int = 1000, print_summary: bool = True) -> None:
        """
        Run SVI to infer the GP model parameters
        Args:
            rng_key: random number generator key
            X: 2D 'feature vector' with :math:`n x num_features` dimensions
            y: 1D 'target vector' with :math:`(n,)` dimensions
            num_steps: number of SVI steps
            print_summary: print summary at the end of sampling
        """
        X = X if X.ndim > 1 else X[:, None]
        self.X_train = X
        self.y_train = y
        # Setup optimizer and SVI
        optim = numpyro.optim.Adam(step_size=0.005, b1=0.5)
        svi = SVI(
            self.model,
            guide=AutoDelta(self.model),
            optim=optim,
            loss=Trace_ELBO(),
            X=X,
            y=y,
        )
        params = svi.run(rng_key, num_steps)[0]
        # Get kernel parameters from the guide
        self.kernel_params = svi.guide.median(params)
        if print_summary:
            self._print_summary()

    def predict(self, rng_key: jnp.ndarray, X_new: jnp.ndarray,
                kernel_params: Optional[Dict[str, jnp.ndarray]] = None,
                n: int = 1000
                ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        """
        Make prediction at X_new points using learned GP hyperparameters
        Args:
            rng_key: random number generator key
            X_new: 2D vector with new/'test' data of :math:`n x num_features` dimensionality
            samples: kernel posterior parameters (optional)
            n: number of samples from the Multivariate Normal posterior
        Returns:
            Center of the mass of sampled means and all the sampled predictions
        """
        if kernel_params is None:
            kernel_params = self.kernel_params
        y_mean, y_sampled = self._predict(rng_key, X_new, kernel_params, n)
        return y_mean, y_sampled

    def _print_summary(self) -> None:
        if isinstance(self.kernel_params, dict):
            print('\nInferred parameters')
            for (k, v) in self.kernel_params.items():
                spaces = " " * (15 - len(k))
                print(k, spaces, jnp.around(v, 4))

    @partial(jit, static_argnames='self')
    def embed(self, X_test: jnp.ndarray) -> jnp.ndarray:
        z = self.feature_extractor(X_test)
        return z


def mlp(embedim: int):
    return hk.Sequential([
        hk.Linear(1000), jax.nn.relu,
        hk.Linear(500), jax.nn.relu,
        hk.Linear(embedim)])
