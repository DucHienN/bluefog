# Copyright 2020 Bluefog Team. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import torch
from torch.autograd import Variable
import bluefog.torch as bf
from bluefog.common import topology_util
import argparse
import matplotlib.pyplot as plt

parser = argparse.ArgumentParser(
    description="PyTorch ImageNet Example",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser.add_argument(
    "--save-plot-file", default='plot.png', help="Saving the plot in the file."
)
parser.add_argument(
    "--plot-interactive", action='store_true', help="Use plt.show() to present the plot."
)
parser.add_argument(
    "--method", type=int, default=0, help="0:exact diffusion. 1:gradient tracking. 2:push-DIGing"
)
args = parser.parse_args()


def finalize_plot():
    plt.savefig(args.save_plot_file)
    if args.plot_interactive:
        plt.show()
    plt.close()


bf.init()

# ================== The problem formulation ====================================
# The logistic regression problem with non-convex regularizer is defined as
#
# min_w (1/n)*\sum_i ln(1 + exp(-y_i*X_i'*w)) + rho*R(w)
# where R(w) = sum_{j=1}^m beta*x_j^2/(1 + beta*x_j^2) is non-convex regularizer.
#
# Each rank i holds a local dataset (X_i, y_i). We expect each rank will converge
# to a consensus solution at which the global gradient \sum_i grad_i = 0.
#
# For reference, see the following papers
#
# [R1] H. Sun and M. Hong, ``Distributed non-convex first-order optimization and
# information processing: lower complexity bounds and rate optimal algorithms'',
# 2019. (Section 7.2)
#
# [R2] A. Antoniadis, I. Gijbels, and M. Nikolova, `` Penalized likelihood regre-
# ssion for generalized linear models with nonquadratic penalities'', 2011
# ===============================================================================

# Generate data for logistic regression (synthesized data)
torch.random.manual_seed(123417 * bf.rank())
m, n = 20, 5
X = torch.randn(m, n).to(torch.double)
w_0 = torch.randn(n, 1).to(torch.double)
y = torch.rand(m, 1).to(torch.double) < 1 / (1+torch.exp(X.mm(w_0)))
y = y.double()
y = 2*y - 1
rho = 1e-3
beta = 1

# ================== Distributed gradient descent ================================
# Calculate the solution with distributed gradient descent:
# x^{k+1} = x^k - alpha * allreduce(local_grad)
# it will be used to verify the solution of various decentralized algorithms.
# ================================================================================
w_opt = Variable(torch.zeros(n, 1).to(torch.double), requires_grad=True)
maxite = 2000
alpha = 1e-1
for i in range(maxite):
    # calculate gradient via pytorch autograd
    loss = torch.mean(torch.log(1 + torch.exp(-y*X.mm(w_opt)))) \
        + torch.sum(rho*beta*w_opt*w_opt/(1+beta*w_opt*w_opt))
    loss.backward()
    grad = bf.allreduce(w_opt.grad.data, name='gradient')  # global gradient

    # distributed gradient descent
    w_opt.data = w_opt.data - alpha*grad
    w_opt.grad.data.zero_()

loss = torch.mean(torch.log(1 + torch.exp(-y*X.mm(w_opt)))) \
    + torch.sum(rho*beta*w_opt*w_opt/(1+beta*w_opt*w_opt))
loss.backward()
grad = bf.allreduce(w_opt.grad.data, name='gradient')  # global gradient

# evaluate the convergence of distributed logistic regression
# the norm of global gradient is expected to 0 (optimality condition)
global_grad_norm = torch.norm(grad, p=2)
print("[DG] Rank {}: global gradient norm: {}".format(
    bf.rank(), global_grad_norm))

# the norm of local gradient is expected not be be close to 0
# this is because each rank converges to global solution, not local solution
local_grad_norm = torch.norm(w_opt.grad.data, p=2)
print("[DG] Rank {}: local gradient norm: {}".format(bf.rank(), local_grad_norm))

# ==================== Exact Diffusion ===========================================
# Calculate the true solution with exact diffusion recursion as follows:
#
# psi^{k+1} = w^k - alpha * grad(w^k)
# phi^{k+1} = psi^{k+1} + w^k - psi^{k}
# w^{k+1} = neighbor_allreduce(phi^{k+1})
#
# Reference:
# K. Yuan, B. Ying, X. Zhao, and A. H. Sayed, ``Exact diffusion for distributed
# optimization and learning -- Part I: Algorithm development'', 2018. (Alg. 1)
# link: https://arxiv.org/abs/1702.05122
# ================================================================================
if args.method == 0:
    w = Variable(torch.zeros(n, 1).to(torch.double), requires_grad=True)
    phi, psi, psi_prev = w.clone(), w.clone(), w.clone()
    alpha_ed = 1e-1  # step-size for exact diffusion
    mse = []
    for i in range(maxite):
        # calculate loccal gradient via pytorch autograd
        loss = torch.mean(torch.log(1 + torch.exp(-y*X.mm(w)))) \
            + torch.sum(rho*beta*w*w/(1+beta*w*w))
        loss.backward()

        # exact diffusion
        psi = w - alpha_ed * w.grad.data
        phi = psi + w.data - psi_prev
        w.data = bf.neighbor_allreduce(phi, name='local variable')
        psi_prev = psi
        w.grad.data.zero_()

        # record convergence
        if bf.rank() == 0:
            mse.append(torch.norm(w.data - w_opt.data, p=2))

    loss = torch.mean(torch.log(1 + torch.exp(-y*X.mm(w)))) \
        + torch.sum(rho*beta*w*w/(1+beta*w*w))
    loss.backward()
    grad = bf.allreduce(w.grad.data, name='gradient')  # global gradient

    # evaluate the convergence of exact diffuion logistic regression
    # the norm of global gradient is expected to be 0 (optimality condition)
    global_grad_norm = torch.norm(grad, p=2)
    print("[ED] Rank {}: global gradient norm: {}".format(
        bf.rank(), global_grad_norm))

    # the norm of local gradient is expected not be be close to 0
    # this is because each rank converges to global solution, not local solution
    local_grad_norm = torch.norm(w.grad.data, p=2)
    print("[ED] Rank {}: local gradient norm: {}".format(
        bf.rank(), local_grad_norm))
    w.grad.data.zero_()

    if bf.rank() == 0:
        plt.semilogy(mse)
        finalize_plot()

# ======================= gradient tracking =====================================
# Calculate the true solution with gradient tracking (GT for short):
#
# w^{k+1} = neighbor_allreduce(w^k) - alpha*q^k
# q^{k+1} = neighbor_allreduce(q^k) + grad(w^{k+1}) - grad(w^k)
# where q^0 = grad(w^0)
#
# Reference:
# [R1] A. Nedic, A. Olshevsky, and W. Shi, ``Achieving geometric convergence
# for distributed optimization over time-varying graphs'', 2017. (Alg. 1)
#
# [R2] G. Qu and N. Li, ``Harnessing smoothness to accelerate distributed
# optimization'', 2018
#
# [R3] J. Xu et.al., ``Augmented distributed gradient methods for multi-agent
# optimization under uncoordinated constant stepsizes'', 2015
#
# [R4] P. Di Lorenzo and G. Scutari, ``Next: In-network nonconvex optimization'',
# 2016
# ================================================================================
if args.method == 1:
    w = Variable(torch.zeros(n, 1).to(torch.double), requires_grad=True)
    loss = torch.mean(torch.log(1 + torch.exp(-y*X.mm(w)))) \
        + torch.sum(rho*beta*w*w/(1+beta*w*w))
    loss.backward()
    q = w.grad.data  # q^0 = grad(w^0)
    w.grad.data.zero_()

    grad_prev = q.clone()
    alpha_gt = 1e-1  # step-size for GT
    mse_gt = []
    for i in range(maxite):
        # w^{k+1} = neighbor_allreduce(w^k) - alpha*q^k
        w.data = bf.neighbor_allreduce(
            w.data, name='local variable w') - alpha_gt * q

        # calculate local gradient
        loss = torch.mean(torch.log(1 + torch.exp(-y*X.mm(w)))) \
            + torch.sum(rho*beta*w*w/(1+beta*w*w))
        loss.backward()
        grad = w.grad.data.clone()    # local gradient at w^{k+1}
        w.grad.data.zero_()

        # q^{k+1} = neighbor_allreduce(q^k) + grad(w^{k+1}) - grad(w^k)
        q = bf.neighbor_allreduce(
            q, name='local variable q') + grad - grad_prev
        grad_prev = grad

        # record convergence
        if bf.rank() == 0:
            mse_gt.append(torch.norm(w.data - w_opt.data, p=2))

    # calculate local and global gradient
    loss = torch.mean(torch.log(1 + torch.exp(-y*X.mm(w)))) \
        + torch.sum(rho*beta*w*w/(1+beta*w*w))
    loss.backward()
    grad = bf.allreduce(w.grad.data, name='gradient')  # global gradient

    # evaluate the convergence of gradient tracking for logistic regression
    # the norm of global gradient is expected to be 0 (optimality condition)
    global_grad_norm = torch.norm(grad, p=2)
    print("[GT] Rank {}: global gradient norm: {}".format(
        bf.rank(), global_grad_norm))

    # the norm of local gradient is expected not be be close to 0
    # this is because each rank converges to global solution, not local solution
    local_grad_norm = torch.norm(w.grad.data, p=2)
    print("[GT] Rank {}: local gradient norm: {}".format(
        bf.rank(), local_grad_norm))
    w.grad.data.zero_()

    if bf.rank() == 0:
        plt.semilogy(mse_gt)
        finalize_plot()

# ======================= Push-DIGing for directed graph =======================
# Calculate the true solution with Push-DIGing:
#
# Reference:
#
# [R1] A. Nedic, A. Olshevsky, and W. Shi, ``Achieving geometric convergence
# for distributed optimization over time-varying graphs'', 2017. (Alg. 2)
# ============================================================================

# In this example, we let A be the data, b be the label
# and x be the solution
if args.method == 2:
    A = X.clone()
    b = y.clone()

    bf.set_topology(topology_util.PowerTwoRingGraph(bf.size()))
    outdegree = len(bf.out_neighbor_ranks())
    indegree = len(bf.in_neighbor_ranks())

    # u, y, v = w[:n], w[n:2*n], w[2n]
    w = torch.zeros(2*n+1, 1).to(torch.double)
    x = Variable(torch.zeros(n, 1).to(torch.double), requires_grad=True)

    loss = torch.mean(torch.log(1 + torch.exp(-b*A.mm(x)))) \
        + torch.sum(rho*beta*x*x/(1+beta*x*x))
    loss.backward()
    grad = x.grad.data.clone()
    w[n:2*n] = grad
    x.grad.data.zero_()

    w[-1] = 1.0
    grad_prev = w[n:2*n].clone()

    bf.win_create(w, name="w_buff", zero_init=True)

    alpha_pd = 1e-1  # step-size for Push-DIGing
    mse_pd = []
    for i in range(maxite):
        if i % 10 == 0:
            bf.barrier()

        w[:n] = w[:n] - alpha_pd*w[n:2*n]
        bf.win_accumulate(
            w, name="w_buff",
            dst_weights={rank: 1.0 / (outdegree + 1)
                         for rank in bf.out_neighbor_ranks()},
            require_mutex=True)
        w.div_(1+outdegree)
        w = bf.win_sync_then_collect(name="w_buff")

        x.data = w[:n]/w[-1]

        loss = torch.mean(torch.log(1 + torch.exp(-b*A.mm(x)))) \
            + torch.sum(rho*beta*x*x/(1+beta*x*x))
        loss.backward()
        grad = x.grad.data.clone()
        x.grad.data.zero_()

        w[n:2*n] += grad - grad_prev
        grad_prev = grad
        if bf.rank() == 0:
            mse_pd.append(torch.norm(x.data - w_opt, p=2))

    bf.barrier()
    w = bf.win_sync_then_collect(name="w_buff")
    x.data = w[:n]/w[-1]

    # calculate local and global gradient
    loss = torch.mean(torch.log(1 + torch.exp(-b*A.mm(x)))) \
        + torch.sum(rho*beta*x*x/(1+beta*x*x))
    loss.backward()
    grad = bf.allreduce(x.grad.data, name='gradient')  # global gradient

    # evaluate the convergence of gradient tracking for logistic regression
    # the norm of global gradient is expected to be 0 (optimality condition)
    global_grad_norm = torch.norm(grad, p=2)
    print("[PD] Rank {}: global gradient norm: {}".format(
        bf.rank(), global_grad_norm))

    # the norm of local gradient is expected not be be close to 0
    # this is because each rank converges to global solution, not local solution
    local_grad_norm = torch.norm(x.grad.data, p=2)
    print("[PD] Rank {}: local gradient norm: {}".format(
        bf.rank(), local_grad_norm))
    x.grad.data.zero_()

    if bf.rank() == 0:
        plt.semilogy(mse_pd)
        finalize_plot()
