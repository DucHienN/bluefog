from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import itertools
import numpy as np
import os
import tensorflow as tf
from bluefog.tensorflow.util import _executing_eagerly, _has_eager
from tensorflow.python.framework import ops
import warnings

import bluefog.tensorflow as bf

if hasattr(tf, 'ConfigProto'):
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True

if hasattr(tf, 'config') and hasattr(tf.config, 'experimental') \
        and hasattr(tf.config.experimental, 'set_memory_growth'):
    gpus = tf.config.experimental.list_physical_devices('GPU')
    for gpu in gpus:
        tf.config.experimental.set_memory_growth(gpu, True)
else:
    if _has_eager:
        # Specifies the config to use with eager execution. Does not preclude
        # tests from running in the graph mode.
        tf.enable_eager_execution(config=config)


class OpsTests(tf.test.TestCase):
    """
    Tests for bluefog/tensorflow/mpi_ops.py
    """

    def __init__(self, *args, **kwargs):
        super(OpsTests, self).__init__(*args, **kwargs)
        warnings.simplefilter('module')
        if _has_eager:
            if hasattr(tf, 'contrib') and hasattr(tf.contrib, 'eager'):
                self.tfe = tf.contrib.eager
            else:
                self.tfe = tf

    def setUp(self):
        bf.init()

    def evaluate(self, tensors):
        if _executing_eagerly():
            return self._eval_helper(tensors)
        sess = ops.get_default_session()
        if sess is None:
            with self.test_session(config=config) as sess:
                return sess.run(tensors)
        else:
            return sess.run(tensors)

    def random_uniform(self, *args, **kwargs):
        if hasattr(tf, 'random') and hasattr(tf.random, 'set_seed'):
            tf.random.set_seed(12345)
            return tf.random.uniform(*args, **kwargs)
        else:
            tf.set_random_seed(12345)
            return tf.random_uniform(*args, **kwargs)

    def test_bluefog_allreduce_cpu(self):
        """Test on CPU that the allreduce correctly sums 1D, 2D, 3D tensors."""
        size = bf.size()
        dtypes = [tf.int32, tf.int64, tf.float16, tf.float32, tf.float64]
        dims = [1, 2, 3]
        for dtype, dim in itertools.product(dtypes, dims):
            with tf.device("/cpu:0"):
                tensor = self.random_uniform(
                    [17] * dim, -100, 100, dtype=dtype)
                summed = bf.allreduce(tensor, average=False)
            multiplied = tensor * size
            max_difference = tf.reduce_max(tf.abs(summed - multiplied))

            # Threshold for floating point equality depends on number of
            # ranks, since we're comparing against precise multiplication.
            if size <= 3 or dtype in [tf.int32, tf.int64]:
                threshold = 0
            elif size < 10:
                threshold = 1e-4
            elif size < 15:
                threshold = 5e-4
            else:
                break

            diff = self.evaluate(max_difference)
            self.assertTrue(diff <= threshold,
                            "hvd.allreduce produces incorrect results")

if __name__ == "__main__":
    tf.test.main()
