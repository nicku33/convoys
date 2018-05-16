import autograd
import emcee
import numpy
from scipy.special import gammainc, gammaincinv
from autograd.scipy.special import expit, gammaln # , gammainc
from autograd.numpy import isnan, exp, dot, log, sum
import scipy.stats
import tensorflow as tf
import warnings
from convoys import tf_utils
from convoys.gamma import gammainc


class RegressionModel(object):
    pass


class GeneralizedGamma(RegressionModel):
    # https://en.wikipedia.org/wiki/Generalized_gamma_distribution
    # Note however that lambda is a^-1 in WP's notation
    # Note also that k = d/p so d = k*p
    def __init__(self, method='MCMC'):
        self._method = method

    def fit(self, X, B, T, W=None, k=None, p=None):
        # Note on using Powell: tf.igamma returns the wrong gradient wrt k
        # https://github.com/tensorflow/tensorflow/issues/17995
        # Sanity check input:
        if W is None:
            W = [1] * len(X)
        XBTW = [(x, b, t, w) for x, b, t, w in zip(X, B, T, W)
                if t > 0 or float(t) not in [0, 1] or w < 0]
        if len(XBTW) < len(X):
            n_removed = len(X) - len(XBTW)
            warnings.warn('Warning! Removed %d entries from inputs where' +
                          'T <= 0 or B not 0/1 or W < 0' % n_removed)
        X, B, T, W = (numpy.array([z[i] for z in XBTW], dtype=numpy.float32)
                      for i in range(4))
        n_features = X.shape[1]

        # Define model
        # Note that scipy.optimize and emcee forces the the parameters to be a vector:
        # (log k, log p, log sigma_alpha, log sigma_beta, a, b, alpha_1...alpha_k, beta_1...beta_k)
        fix_k, fix_p = k, p

        def log_likelihood(x):
            k = exp(x[0]) if fix_k is None else fix_k
            p = exp(x[1]) if fix_p is None else fix_p
            log_sigma_alpha = x[2]
            log_sigma_beta = x[3]
            a = x[4]
            b = x[5]
            alpha = x[6:6+n_features]
            beta = x[6+n_features:6+2*n_features]
            lambd = exp(dot(X, alpha)+a)
            c = expit(dot(X, beta)+b)

            # PDF: p*lambda^(k*p) / gamma(k) * t^(k*p-1) * exp(-(x*lambda)^p)
            log_pdf = \
                      log(p) + (k*p) * log(lambd) \
                      - gammaln(k) + (k*p-1) * log(T) \
                      - (T*lambd)**p
            cdf = gammainc(k, (T*lambd)**p)

            LL_observed = log(c) + log_pdf
            LL_censored = log((1-c) + c * (1 - cdf))

            LL_data = sum(
                W * B * LL_observed +
                W * (1 - B) * LL_censored, 0)

            # TODO: explain these prior terms
            LL_prior_a = -log_sigma_alpha**2 - dot(alpha, alpha) / (2*exp(log_sigma_alpha)**2) - n_features*log_sigma_alpha
            LL_prior_b = -log_sigma_beta**2 - dot(beta, beta) / (2*exp(log_sigma_beta)**2) - n_features*log_sigma_beta

            LL = LL_prior_a + LL_prior_b + LL_data

            if isnan(LL):
                return -numpy.inf
            else:
                if isinstance(x, numpy.ndarray):
                    print('%9.6e %9.6e %9.6e %9.6e -> %9.6e %30s' % (k, p, exp(log_sigma_alpha), exp(log_sigma_beta), LL, ''), end='\r')
                return LL

        x0 = numpy.zeros(6+2*n_features)
        print('\nFinding MAP:')
        neg_log_likelihood = lambda x: -log_likelihood(x)
        res = scipy.optimize.minimize(
            neg_log_likelihood,
            x0,
            jac=autograd.grad(neg_log_likelihood),
            method='SLSQP',
        )
        x0 = res.x
        if self._method == 'MCMC':
            dim, = x0.shape
            nwalkers = 5*dim
            sampler = emcee.EnsembleSampler(
                nwalkers=nwalkers,
                dim=dim,
                lnpostfn=log_likelihood)
            mcmc_initial_noise = 1e-3
            p0 = [x0 + mcmc_initial_noise * numpy.random.randn(dim) for i in range(nwalkers)]
            nburnin = 20
            nsteps = numpy.ceil(1000. / nwalkers)
            print('\nStarting MCMC with %d walkers and %d steps:' % (nwalkers, nburnin+nsteps))
            sampler.run_mcmc(p0, nburnin+nsteps)
            print('\n')
            data = sampler.chain[:,nburnin:,].reshape((-1, dim)).T
        else:
            # Should be very easy to support, we just need to modify tf_utils.predict a bit
            data = x0
            raise Exception('TODO: this is not supported yet')

        # The `data` array is either 1D (in the case of MAP) or 2D (in the case of MCMC)
        self.params = {
            'k': exp(data[0]),
            'p': exp(data[1]),
            'a': data[4],
            'b': data[5],
            'alpha': data[6:6+n_features].T,
            'beta': data[6+n_features:6+2*n_features].T,
            }

    def cdf(self, x, t, ci=None):
        t = numpy.array(t)
        lambd = exp(dot(self.params['alpha'], x) + self.params['a'])
        c = expit(dot(self.params['beta'], x) + self.params['b'])
        M = c * gammainc(
            self.params['k'],
            numpy.multiply.outer(t, lambd)**self.params['p'])
        return tf_utils.predict(M, ci)

    def rvs(self, x, n_curves=1, n_samples=1, T=None):
        # Samples values from this distribution
        # T is optional and means we already observed non-conversion until T
        if T is None:
            T = numpy.zeros((n_curves, n_samples))
        else:
            assert T.shape == (n_curves, n_samples)
        B = numpy.zeros((n_curves, n_samples), dtype=numpy.bool)
        C = numpy.zeros((n_curves, n_samples))
        n = len(self.params['k'])
        for i in range(n_curves):
            k = self.params['k'][i%n]
            p = self.params['k'][i%n]
            lambd = exp(dot(x, self.params['alpha'][i%n]) + self.params['a'][i%n])
            c = expit(dot(x, self.params['beta'][i%n]) + self.params['b'][i%n])
            z = numpy.random.uniform(size=(n_samples,))
            cdf_now = c * gammainc(
                k,
                numpy.multiply.outer(T[i], lambd)**p)  # why is this outer?
            adjusted_z = cdf_now + (1 - cdf_now) * z
            B[i] = (adjusted_z < c)
            y = adjusted_z / c
            x = gammaincinv(k, y)
            # x = (t * lambd)**p
            C[i] = x**(1./p) / lambd
            C[i][~B[i]] = 0

        return B, C


class Exponential(GeneralizedGamma):
    def fit(self, X, B, T, W=None):
        super(Exponential, self).fit(X, B, T, W, k=1, p=1)


class Weibull(GeneralizedGamma):
    def fit(self, X, B, T, W=None):
        super(Weibull, self).fit(X, B, T, W, k=1)


class Gamma(GeneralizedGamma):
    def fit(self, X, B, T, W=None):
        super(Gamma, self).fit(X, B, T, W, p=1)
