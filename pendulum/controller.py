import numpy as np
from scipy import spatial
import cvxpy as cp
from collections import deque
from scipy.signal import cont2discrete
from scipy import optimize
import matplotlib.pyplot as plt
from sklearn.gaussian_process import GaussianProcessRegressor
import sklearn.gaussian_process
import time


class Controller(object):
    '''
    Class template for pendulum controller
    '''
    def __init__(self, init_state):
        self.init_state = init_state
    
    def policy(self, state):
        '''
        A controller must have a policy action.
        
        Parameters
        ----------
        state: (float, float, float float)
            The current system state
        
        Returns
        -------
        float
            The controller action, in force applied to the cart.
        '''
        raise NotImplementedError

 

class MPCController(Controller):
    def __init__(self, pendulum, T, dt, u_max=1000, plotting=False):
        self.pendulum = pendulum 
        self.plotting = plotting
        self.setpoint = 0
        
        # System A 
        # nxn

        A = np.array([
            [0, 1, 0, 0],
            [0, 0, (pendulum.g * pendulum.m)/pendulum.M, 0],
            [0, 0, 0, 1],
            [0, 0, pendulum.g/pendulum.l + pendulum.g * pendulum.m/(pendulum.l*pendulum.M), 0]
        ])

        # Input B
        # n x p

        B = np.array([
            [0], 
            [1/pendulum.M],
            [0],
            [1/pendulum.M*pendulum.l]
            
        ])

        # Output C
        # q x n
        # 
        # (blank for now)

        C = np.zeros((1, A.shape[0]))

        # Feedthrough D
        # q x p
        # 
        # (blank for now)
        
        D = np.zeros((1, 1))

        sys_disc = cont2discrete((A,B,C,D), dt, method='zoh')
        self.A = sys_disc[0]
        self.B = sys_disc[1]
        
        self.T = T
        self.u_max = u_max

        self.planned_u = []
        self.planned_state = []

    def policy(self, state, t, dt):
        x = cp.Variable((4, self.T+1))
        u = cp.Variable((1, self.T))
        cost = 0
        constr = []
        for t in range(self.T):
            constr.append(x[:, t + 1] == self.A @ x[:, t] + self.B @ u[:, t])
            constr.append(cp.abs(u[:, t]) <= self.u_max)
        cost += cp.sum_squares(x[2,self.T])
        cost += 1e4*cp.sum_squares(u[0,self.T-1])
        constr += [x[:, 0] == state]
        problem = cp.Problem(cp.Minimize(cost), constraints=constr)
        problem.solve(verbose=False, solver='ECOS')
        action = u[0,0].value

        # dump estimate info
        self.planned_state = x[:,:].value
        self.planned_u = u[0,:].value

        # print('x: {}'.format(x[:,:].value))
        return action       

    def init_plot(self):
        prediction = plt.figure()
        return prediction

    def update_plot(self, figure):
        plt.clf()
        ax0 = figure.add_subplot(211)
        ax1 = figure.add_subplot(212)
        # States
        ax0.plot(self.planned_state[0], label=r'$x$')
        ax0.plot(self.planned_state[1], label=r'$\dot{x}$') 
        ax0.plot(self.planned_state[2], label=r'$\theta$')
        ax0.plot(self.planned_state[3], label=r'$\dot{\theta}$')   
        ax0.axhline(y = self.setpoint, color='k', drawstyle='steps', linestyle='dotted', label=r'set point')
        ax0.set_ylabel("state")
        ax0.legend()
        # Control Actions
        ax1.plot(self.planned_u, label=r'$u$')
        ax1.set_ylabel("force")
        ax1.legend()
        plt.draw()
        plt.pause(0.0001)


class NoController(Controller):
    def __init__(self):
        pass
    
    def policy(self, state, t, dt):
        return 0

class MPCWithGPR(Controller):
    def __init__(self, pend, dt, measure_n=10, window=8):
        # prior observations
        self.M = window
        self.pend = pend
        self.measure_n = measure_n

        # GPR properties
        self.lenscale = 1
    
        # prior points
        self.priors = deque()

        # linear model
        A = np.array([
            [0, 1, 0, 0],
            [0, 0, (pend.g * pend.m)/pend.M, 0],
            [0, 0, 0, 1],
            [0, 0, pend.g/pend.l + pend.g * pend.m/(pend.l*pend.m), 0]])
        B = np.array([
            [0], 
            [1/pend.M], 
            [0], 
            [1/(pend.M * pend.l)]])
        C = np.zeros((1, A.shape[0]))
        D = np.zeros((1, 1))
        sys_disc = cont2discrete((A,B,C,D), dt, method='zoh')
        self.A = sys_disc[0]
        self.B = sys_disc[1]
        self.tick = 0

    def policy(self, state, t, dt):
        data = {}
        if self.tick > self.M +1:
            # delete oldest from prior window
            self.priors.popleft()
            # first val = most recent
            xk1 = np.atleast_2d(self.priors)[:-1,:]
            xk  = np.atleast_2d(self.priors)[1:,:]
            linear_xk = np.dot(xk1[:,:4], self.A) + np.dot(np.atleast_2d(xk1[:,4]).T, self.B.T)
            y = linear_xk - xk[:,:4] # M x n_d
            z = xk1 # M x n_z
            # create cov matrix
            L = self.create_prior_matr(z, y, self.lenscale)
            # predict uncertainty
            mu, sig = self.make_prediction(L, self.lenscale, z, y, state, self.prior_action)
            # make linear, nonlinear predictions
            lpred = np.dot(np.atleast_2d(state), self.A)
            nlpred = lpred - mu

            # make n-ahead linear, nonlinear predictions
            lpred_n = np.atleast_2d(state)
            nlpred_n = np.atleast_2d(state)
            for _ in range(self.measure_n):
                lpred_n = np.dot(lpred_n, self.A)
                mu, sig = self.make_prediction(L, self.lenscale, z, y, nlpred_n, self.prior_action)
                nlpred_n = np.dot(nlpred_n, self.A) - mu

            # write data
            data['mu'] = mu
            data['sigma'] = sig
            data['lpred'] = lpred
            data['nlpred'] = nlpred
            data['lpred_n'] = lpred_n
            data['nlpred_n'] = nlpred_n
            data['zeros'] = np.zeros(np.atleast_2d(state).shape)
        else:
            stateshape = np.atleast_2d(state).shape
            data['mu'] = np.zeros(stateshape)
            data['sigma'] = np.zeros(stateshape)
            data['lpred'] = np.zeros(stateshape)
            data['nlpred'] = np.zeros(stateshape)
            data['lpred_n'] = np.zeros(stateshape)
            data['nlpred_n'] = np.zeros(stateshape)
            data['zeros'] = np.zeros(np.atleast_2d(state).shape)

        # no action for now
        action = 0
        # build prior window
        self.prior_action = action
        self.priors.append(list(state) + [action])
        # we increment tick every time we take ctrl action
        self.tick += 1
        return action, data
    
    def apply_kernel(self, x1, x2=None, lenscale=1):
        if x2 is None:
            diff = spatial.distance.pdist(x1, metric='sqeuclidean')
            gram = np.exp(diff/lenscale * -0.5)
            gram = spatial.distance.squareform(gram)
            np.fill_diagonal(gram, 1)
        else:
            diff = spatial.distance.cdist(x1, x2, metric='sqeuclidean')
            gram = np.exp(diff/lenscale * - 0.5)
        return gram

    def ll_loss(self, z, y, theta):
        K = self.apply_kernel(z, a=theta)
        K[np.diag_indices_from(K)] += np.var(y, axis=1) + 1e-8
        L = np.linalg.cholesky(K)
        alpha = np.linalg.solve(L.T, np.linalg.solve(L, y))
        a = -0.5 * np.dot(y.T, alpha)
        b = -0.5 * np.log(np.trace(L))
        c = -0.5 * L.shape[0] * np.log(np.pi * 2)
        ll = a + b + c
        return ll.sum()
    
    def optimize(self, z, y):
        theta = 1
        def obj_func(theta, z, y):
            return -self.ll_loss(z, y, theta)

        results = optimize.minimize(
            obj_func,
            theta,
            (z, y),
            method='L-BFGS-B',
            options={'disp': True}
        )
        print('\ttheta={}'.format(results['x']))
        return(results['x'])

    def create_prior_matr(self, z, y, l):
        '''
        Return cholesky of cov matrix K built from 
        z, y, and l
        '''
        # length scale
        K = self.apply_kernel(z, lenscale=l)
        K[np.diag_indices_from(K)] += np.var(y, axis=1) + 1e-9
        L = np.linalg.cholesky(K)
        return L

    def make_prediction(self, L, l, z, y, x, u):
        '''
        L: cholesky of cov matrix K
        l: length parameter of matrix
        z: prior   x, u
        y: prior g(x, u)

        x: state
        u: control input
        '''
        # prefill
        mu = np.zeros((1,np.shape(y)[1]))
        sigma = np.zeros((1,np.shape(y)[1]))
        z_new = np.empty((1,5))
        z_new[:, :4] = np.asarray(x)
        z_new[:, 4] = np.asarray(u)
        # we train one model for each dim of the output y.
        for a in range(y.shape[1]):
            alpha = np.linalg.solve(L.T, np.linalg.solve(L, y[:,a]))
            # mean
            z_star = self.apply_kernel(z_new, z, lenscale=l)
            mu[0,a] = z_star.dot(alpha)
            # variance
            v = np.linalg.solve(L, z_star.T)
            sigma[0,a] = self.apply_kernel(z_new, z_new, lenscale=l) - np.dot(v.T, v)
        # sigma = np.sqrt(sigma)
        return mu, sigma

        
class BangBang(Controller):
    def __init__(self, setpoint, magnitude):
        self.setpoint = setpoint
        self.magnitude = magnitude
    
    def policy(self, state, t):
        error = state[2] - self.setpoint
        if error > 0.1 and state[2] < np.pi/4:
            return self.magnitude
        elif error < -0.1 and state[2] > -np.pi/4:
            return -self.magnitude
        else:
            return 0