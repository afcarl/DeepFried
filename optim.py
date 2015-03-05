#!/usr/bin/env python3

from DeepFried.util import batched

import numpy as _np
import theano as _th
import theano.tensor as _T


class StreaMiniOptimizer(object):
    """
    This is an optimizer that works through minibatches of the dataset, each
    minibatch being uploaded onto the GPU each time.

    This is slower than moving the whole dataset on the GPU once and addressing
    each slices of it, but it allows for larger datasets to fit on the GPU as
    well as "infinite" data augmentation.
    """


    def __init__(self, batchsize, model, cost, extra_outs=None, X=None, t=None):
        """
        Initializes the things that are common amongst all streaming minibatch
        optimizers.

        - `batchsize`: The number of samples in a minibatch.
        - `model`: The model. This should be an object with at least:
            - `make_input(name='')`: a function which returns a symbolic
                variable of the correct dimensions for serving as input.
            - `train_expr(X)`: a function which returns the symbolic output
                of the model, during training, given symbolic model input `X`.
            - `params`: an iterable containing all trainable parameters.
        - `cost`: The cost. This should be an object with at least:
            - `make_target(name='')`: a function which returns a symbolic
                variable of the correct dimensions for serving as target.
            - `cost_expr(Y, t)`: a function which returns the symbolic cost
                of the output `Y` wrt. the targets `t`.
            - `aggregate_batches(costs)`: a function which returns the
                aggregation of the `costs` of each minibatch.
        - `extra_outs`: TODO
        - `X`: A symbolic variable to be used as input to the model. If `None`,
            a new one will be created.
        - `t`: A symbolic variable to be used as targets for the cost. If
            `None`, a new one will be created.
        """
        self.model = model
        self.cost = cost
        self.batchsize = batchsize

        if extra_outs is not None:
            self.outs = extra_outs
        else:
            self.outs = []

        if X is None:
            self.X = self.model.make_input()
        elif isinstance(X, str):
            self.X = self.model.make_input(X)
        else:
            self.X = X

        if t is None:
            self.t = self.cost.make_target()
        elif isinstance(t, str):
            self.t = self.cost.make_target(t)
        else:
            self.t = t

        self.cost_expr = self.cost.cost_expr(self.model.train_expr(self.X), self.t)
        self.outs = [self.cost_expr] + self.outs


    def fit_epoch(self, X, t, aug=None, batchsize=None, *args, **kwargs):
        """
        Trains the model for one full epoch by iterating through minibatches.

        - `X`: A numpy array containing the data. The first dimension should be
               the datapoints, i.e. X.shape[0] == ndata, and any remaining
               dimensions should fit the model's expected input shape.
        - `t`: The target values where the first dimension should be the
               datapoints, just like for `X`.
        - `aug`: An optional data augmentation pipeline that can transform each
                 sample in the minibatch individually.
        - `batchsize`: Optionally override the batchsize given at construction.

        Any remaining arguments will be passed on to the optimization function;
        this can be used to pass values such as learning-rate, momentum etc.
        """
        costs = []
        rests = []

        bs = batchsize or self.batchsize

        # Go through the training in minibatches. Note that the last batch
        # may be smaller than the batchsize.
        for bx, bt in batched(bs, X, t):
            # Potentially generate a new augmentation on-the-fly.
            if aug:
                bx = aug.augbatch_train(bx, bt)

            # Uploads to the GPU, does the forward pass,
            # the backward pass *and* the weight updates!
            cost, *rest = self.fn_train(bx, bt, *args, **kwargs)

            # Collect stats over the batches, so we can average.
            costs.append(cost)
            rests.append(rest)

        # Average the stats over the batches.
        return self.cost.aggregate_batches(costs), None  # TODO


class StreaMiniSGD(StreaMiniOptimizer):
    """
    Vanilla Stochastic Gradient Descent on minibatches. The training is quite
    simple:

        p_{e+1} = p_e - lr * ∇p_e

    Additional parameters added to `fit_epoch`:

    - `lrate`: The learning-rate.
    """

    def __init__(self, batchsize, model, cost, *args, **kwargs):
        """
        See `StreaMiniOptimizer` for details on the arguments.
        """
        super(StreaMiniSGD, self).__init__(batchsize, model, cost, *args, **kwargs)

        self.sh_learningrate = _T.scalar('lrate')

        # For SGD, training is quite simple:
        # p_e+1 = p_e - lr * grad(p_e)
        g = _T.grad(cost=self.cost_expr, wrt=self.model.params)
        self.fn_train = _th.function(
            inputs=[self.X, self.t, self.sh_learningrate],
            outputs=self.outs,
            updates=[(p, p - self.sh_learningrate * gp) for p, gp in zip(self.model.params, g)],
            name="StreaMiniSGD train"
        )


class StreaMiniMomentum(StreaMiniOptimizer):
    """
    TL;DR: Nesterov allows for larger momentum to be used, making it better.
           Very finicky parameter-selection.

    Implements both the "Classical Momentum (CM)" and "Nesterov's
    Accelerated Gradient (NAG)" which are explained in further detail in

    "On the importance of initialization and momentum in deep learning"

    But the equation for NAG has been reshuffled by Nicolas Boulanger in

    https://github.com/lisa-lab/pylearn2/pull/136#issuecomment-10381617

    for easier implementation in Theano. The updates are:

        v_{e+1} = mom * v_e - lr * ∇p_e
        p_{e+1} = p_e + v_{e+1}

    for CM, and

        p_{e+1} = p_e + mom * v_{e+1} - lr * ∇p_e

    for Nicolas' reformulated NAG.

    Additional parameters added to `fit_epoch`:

    - `lrate`: The learning-rate.
    - `momentum`: The momentum, defaulting to the one passed at construction.
    """

    def __init__(self, batchsize, model, cost, momentum, nesterov=False, *args, **kwargs):
        """
        See `StreaMiniOptimizer` for details on the arguments.

        - `momentum`: The amount of momentum to use, typically something around
            0.9, 0.95 or 0.99. This value sets the default, but it can also
            be overridden in each individual call to `fit_epoch`.
        - `nesterov`: If `True`, Nesterov's momentum (NAG) is used instead
            of classical momentum (CM).
        """
        super(StreaMiniMomentum, self).__init__(batchsize, model, cost, *args, **kwargs)

        self.sh_learningrate = _T.scalar('lrate')
        self.sh_momentum = _T.scalar('momentum')

        # For momentum, we need a "mirror" of each parameter, which keeps track
        # of the "velocity" of that parameter during training.
        self.sh_v = [
            _th.shared(_np.zeros_like(p.get_value()), broadcastable=p.broadcastable, name='v_'+p.name)
            for p in model.params
        ]

        g = _T.grad(cost=self.cost_expr, wrt=self.model.params)

        updates = []
        for sh_p, gp, sh_v in zip(self.model.params, g, self.sh_v):
            v = self.sh_momentum * sh_v - self.sh_learningrate * gp
            updates.append((sh_v, v))

            if not nesterov:
                updates.append((sh_p, sh_p + v))
            else:
                updates.append((sh_p, sh_p + self.sh_momentum * v - self.sh_learningrate * gp))

        self.fn_train = _th.function(
            inputs=[self.X, self.t, self.sh_learningrate,
                _th.Param(self.sh_momentum, momentum)],
            outputs=self.outs,
            updates=updates,
            name="StreaMiniMomentum train"
        )