import jax
import jax.numpy as np
from jax.random import PRNGKey
from ..glm import glm, GLM, log_link, logit_link, Gamma, Beta
from functools import partial
import pandas as pd
import numpyro
import numpyro.distributions as dist

from ..compartment import SEIRDModel
from .util import observe_normal, observe, observe_nb2, ExponentialRandomWalk, LogisticRandomWalk, frozen_random_walk, clean_daily_obs
from .base import SEIRDBase, getter

import numpy as onp


"""
************************************************************
SEIRD model
************************************************************
"""

class SEIRD(SEIRDBase):    
    
    def __call__(self,
                 T = 50,
                 N = 1e5,
                 T_future = 0,
                 E_duration_est = 4.0,
                 I_duration_est = 2.0,
                 R0_est = 3.0,
                 beta_shape = 1.,
                 sigma_shape = 100.,
                 gamma_shape = 100.,
                 det_prob_est = 0.3,
                 det_prob_conc = 50.,
                 confirmed_dispersion=0.3,
                 death_dispersion=0.3,
                 rw_scale = 2e-1,
                 forecast_rw_scale = 0.,
                 drift_scale = None,
                 num_frozen=0,
                 rw_use_last=1,
                 confirmed=None,
                 death=None,
                 place_data=None):

        '''
        Stochastic SEIR model. Draws random parameters and runs dynamics.
        '''        
                
        # Sample initial number of infected individuals
        I0 = numpyro.sample("I0", dist.Uniform(0, 0.02*N))
        E0 = numpyro.sample("E0", dist.Uniform(0, 0.02*N))
        H0 = numpyro.sample("H0", dist.Uniform(0, 1e-3*N))
        D0 = numpyro.sample("D0", dist.Uniform(0, 1e-3*N))


        # Sample dispersion parameters around specified values

        death_dispersion = numpyro.sample("death_dispersion", 
                                           dist.TruncatedNormal(low=0.1,
                                                                loc=death_dispersion, 
                                                                scale=0.15))


        confirmed_dispersion = numpyro.sample("confirmed_dispersion", 
                                              dist.TruncatedNormal(low=0.1,
                                                                   loc=confirmed_dispersion, 
                                                                   scale=0.15))
        if confirmed is None:
            confirmed0, confirmed = (None, None)
            d={'t':[0]}
        else:
            confirmed0 = confirmed[0]
            confirmed = clean_daily_obs(onp.diff(confirmed)) 
            d={'t':onp.arange(len(confirmed))}

        if death is None:
            death0, death = (None, None)
        else: 
            death0 = death[0]
            death = clean_daily_obs(onp.diff(death))
        place_data = pd.DataFrame()
        R0_glm = GLM("1 + cr(t,df=3)", 
                 d, 
                 log_link,
                 partial(Gamma, var=1),
                 prior = dist.Normal(0, 1),
                 guess=3.5,
                 name="R0")
        R0 = R0_glm.sample(shape=(-1))[0]

        
        # Sample parameters
        sigma = numpyro.sample("sigma", 
                               dist.Gamma(sigma_shape, sigma_shape * E_duration_est))

        gamma = numpyro.sample("gamma", 
                                dist.Gamma(gamma_shape, gamma_shape * I_duration_est))


        beta0 = R0*gamma#numpyro.sample("beta0",
                 #              dist.Gamma(beta_shape, beta_shape * I_duration_est/R0_est))

        det_prob0 = numpyro.sample("det_prob0", 
                                   dist.Beta(det_prob_est * det_prob_conc,
                                            (1-det_prob_est) * det_prob_conc))

        det_prob_d = numpyro.sample("det_prob_d", 
                                    dist.Beta(.9 * 100,
                                              (1-.9) * 100))

        death_prob = numpyro.sample("death_prob", 
                                    dist.Beta(.01 * 100,
                                              (1-.01) * 100))

        death_rate = numpyro.sample("death_rate", 
                                    dist.Gamma(10, 10 * 10))

        if drift_scale is not None:
            drift = numpyro.sample("drift", dist.Normal(loc=0, scale=drift_scale))
        else:
            drift = 0


        x0 = SEIRDModel.seed(N=N, I=I0, E=E0, H=H0, D=D0)
        numpyro.deterministic("x0", x0)

        # Split observations into first and rest
            
        
        # First observation
        with numpyro.handlers.scale(scale_factor=0.5):
            y0 = observe_normal("dy0", x0[6], det_prob0, confirmed_dispersion, obs=confirmed0)
            
        with numpyro.handlers.scale(scale_factor=2.0):
            z0 = observe_normal("dz0", x0[5], det_prob_d, death_dispersion, obs=death0)
        params = (beta0, 
                  sigma, 
                  gamma, 
                  rw_scale, 
                  drift, 
                  det_prob0, 
                  confirmed_dispersion, 
                  death_dispersion,
                  death_prob, 
                  death_rate, 
                  det_prob_d)

        beta, det_prob, x, y = self.dynamics(T, 
                                                params, 
                                                x0,
                                                num_frozen = num_frozen,
                                                confirmed = confirmed,
                                                death = death)

        x = np.vstack((x0, x))
        y = np.append(y0, y)

        if T_future > 0:
            d_future={'t':onp.arange(T,T+T_future)}
            R0_future = R0_glm.sample(d_future, name="R0_future", shape=(-1))[0]
            beta_future = R0_future * gamma
            #beta_future = np.append(beta[-1],beta_future)
            params = (beta_future, 
                      sigma, 
                      gamma, 
                      forecast_rw_scale, 
                      drift, 
                      det_prob[-rw_use_last:].mean(),
                      confirmed_dispersion, 
                      death_dispersion,
                      death_prob, 
                      death_rate, 
                      det_prob_d)

            beta_f, det_rate_rw_f, x_f, y_f = self.dynamics(T_future+1, 
                                                                 params, 
                                                                 x[-1,:],
                                                                 suffix="_future")

            x = np.vstack((x, x_f))
            y = np.append(y, y_f)

        return beta, x, y, det_prob, death_prob
    
    
    def dynamics(self, T, params, x0, num_frozen=0, confirmed=None, death=None, suffix=""):
        '''Run SEIRD dynamics for T time steps'''

        beta0, \
        sigma, \
        gamma, \
        rw_scale, \
        drift, \
        det_prob0, \
        confirmed_dispersion, \
        death_dispersion, \
        death_prob, \
        death_rate, \
        det_prob_d = params

        #rw = frozen_random_walk("rw" + suffix,
         #                       num_steps=T-1,
          #                      num_frozen=num_frozen)
        
        beta = beta0#numpyro.deterministic("beta", beta0 * np.exp(rw_scale*rw))
        beta = numpyro.sample("beta"+suffix, dist.Normal(beta0,.01)) 
        det_prob = numpyro.sample("det_prob" + suffix,
                                  LogisticRandomWalk(loc=det_prob0, 
                                                     scale=rw_scale/100, 
                                                     drift=0,
                                                     num_steps=T-1))

        # Run ODE
        x = SEIRDModel.run(T, x0, (beta, sigma, gamma, death_prob, death_rate))

        numpyro.deterministic("x" + suffix, x[1:])

        x_diff = np.diff(x, axis=0)

        # Noisy observations
        with numpyro.handlers.scale(scale_factor=0.5):
            y = observe_normal("dy" + suffix, x_diff[:,6], det_prob, confirmed_dispersion, obs = confirmed)   

        with numpyro.handlers.scale(scale_factor=2.0):
            z = observe_normal("dz" + suffix, x_diff[:,5], det_prob_d, death_dispersion, obs = death)  
        
        return beta, det_prob, x, y

    
    
    
    
    

    dy = getter('dy')
    dz = getter('dz')
    
    def y0(self, **args):
        return self.z0(**args)

    
    def y(self, samples, **args):
        '''Get cumulative cases from incident ones'''
        
        dy = self.dy(samples, **args)
        
        y0 = np.zeros(dy.shape[0])
        if args.get('forecast'):
            y0 = self.y(samples, forecast=False)[:,-1]
 
        return y0[:,None] + onp.cumsum(dy, axis=1)


    def z0(self, **args):
        return self.z0(**args)

    
    def z(self, samples, **args):
        '''Get cumulative deaths from incident ones'''
        
        dz = self.dz(samples, **args)
        
        z0 = np.zeros(dz.shape[0])
        if args.get('forecast'):
            z0 = self.z(samples, forecast=False)[:,-1]
 
        return z0[:,None] + onp.cumsum(dz, axis=1)
