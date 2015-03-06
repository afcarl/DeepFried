#!/usr/bin/env python3

import numpy as _np
import numbers


def tuplize(what):
    """
    If `what` is a tuple, return it as-is, otherwise put it into a tuple.
    """
    if isinstance(what, tuple):
        return what
    else:
        return (what,)


def maybetuple(what):
    """
    Transforms `what` into a tuple, except if it's of length one.
    """
    t = tuple(what)
    return t if len(t) > 1 else t[0]


def batched(batchsize, *args):
    """
    A generator function which goes through all of `args` together,
    but in batches of size `batchsize` along the first dimension.

    batched_padded(3, np.arange(10), np.arange(10))

    will yield sub-arrays of the given ones four times, the fourth one only
    containing a single value.
    """

    assert(len(args) > 0)

    n = args[0].shape[0]

    # Assumption: all args have the same 1st dimension as the first one.
    assert(all(x.shape[0] == n for x in args))

    # First, go through all full batches.
    for i in range(n // batchsize):
        yield maybetuple(x[i*batchsize:(i+1)*batchsize] for x in args)

    # And now maybe return the last batch.
    rest = n % batchsize
    if rest != 0:
        yield maybetuple(x[-rest:] for x in args)


# Blatantly "inspired" by sklearn, for when that's not available.
def check_random_state(seed):
    """
    Turn `seed` into a `np.random.RandomState` instance.

    - If `seed` is `None`, return the `RandomState` singleton used by `np.random`.
    - If `seed` is an `int`, return a new `RandomState` instance seeded with `seed`.
    - If `seed` is already a `RandomState` instance, return it.
    - Otherwise raise `ValueError`.
    """
    if seed is None or seed is _np.random:
        return _np.random.mtrand._rand

    if isinstance(seed, (numbers.Integral, _np.integer)):
        return _np.random.RandomState(seed)

    if isinstance(seed, _np.random.RandomState):
        return seed

    raise ValueError('{!r} cannot be used to seed a numpy.random.RandomState instance'.format(seed))
