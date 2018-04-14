import time
import random
import pandas as pd

import torch
from torch.autograd import Variable
from sklearn.utils import shuffle


def to_32(arr):
    if 'int' in str(arr.dtype):
        return arr.astype('int64')
    else:
        return arr.astype('float32')


def chunks(batchsize, *arrs, cuda=False):
    n = batchsize
    lens = [arr.shape[0] for arr in arrs]
    err = "Not all arrays are of same shape"
    length = lens[0]
    assert all(length == l for l in lens), err
    for i in range(0, length, n):
        var = [Variable(torch.from_numpy(to_32(arr[i:i + n])))
               for arr in arrs]
        if cuda:
            var = [v.cuda() for v in var]
        yield var


def chunk_shuffle(batchsize, *arrs):
    n = batchsize
    lens = [arr.shape[0] for arr in arrs]
    err = "Not all arrays are of same shape"
    length = lens[0]
    assert all(length == l for l in lens), err
    for i in range(0, length, n):
        yield [Variable(torch.from_numpy(arr[i:i + n])) for arr in arrs]


class Trainer(object):
    def __init__(self, model, optimizer, callbacks={}, seed=42, cuda=False,
                 print_every=25, batchsize=2048, window=500, clip=None,
                 grad_norm=False, backward_kwargs={}):
        self.model = model
        if cuda:
            self.model = self.model.cuda()
        self.optimizer = optimizer
        self.callbacks = callbacks
        self.previous_log = []
        self.log = []
        self._epoch = 0
        self._iteration = 0
        self.seed = seed
        self.print_every = print_every
        self.batchsize = batchsize
        self.window = window
        self.clip = clip
        self.cuda = cuda
        self.backward_kwargs = backward_kwargs
        self.grad_norm = grad_norm

    def fit(self, *args):
        # args is X1, X2,...Xn, Yn
        self.model.train(True)
        self._iteration = 0
        rs = random.randint(0, 100000)
        args = shuffle(*args, random_state=rs)
        # , random_state=self.seed + self._epoch)
        for batch in chunks(self.batchsize, *args, cuda=self.cuda):
            start = time.time()
            self.optimizer.zero_grad()
            pred = self.model.forward(*batch)
            loss = self.model.loss(pred, *batch)
            scalar = sum(loss)
            scalar.backward(**self.backward_kwargs)
            if self.clip:
                torch.nn.utils.clip_grad_norm(self.model.parameters(),
                                              self.clip)
            self.optimizer.step()
            stop = time.time()
            kwargs = {f'loss_{i}': l.data[0] for i, l in enumerate(loss)}
            if self.grad_norm:
                grad_norm = max(p.grad.data.abs().max()
                                for p in self.model.parameters()
                                if p.grad is not None)
                kwargs['grad_norm_max'] = grad_norm
            self.run_callbacks(batch, pred, train=True, iter_time=stop-start,
                               **kwargs)
            if self._iteration % self.print_every == 0:
                self.print_log(header=self._iteration == 0)
            self._iteration += 1
        self._epoch += 1
        self.previous_log.extend(self.log)
        self.log = []
        self.model.train(False)

    def fit_sequence(self, inputs, labels, lengths):
        args = (inputs, labels, lengths)
        for input, label, length in chunk_shuffle(self.batchsize, *args):
            start = time.time()
            for frame in range(length.max()):
                self.optimizer.zero_grad()
                input_frame = input[:, frame]
                label_frame = label[:, frame]
                pred_frame = self.model.forward(input_frame)
                loss = self.model.loss(pred_frame, label_frame)
                loss.backward()
                if self.clip:
                    torch.nn.utils.clip_grad_norm(self.model.parameters(),
                                                  self.clip)
                self.optimizer.step()
            stop = time.time()
            # We estimate AUC just on the last item
            self.run_callbacks([input_frame, label_frame], pred_frame,
                               loss=loss.data[0],
                               train=True, iter_time=stop-start)
            if self._iteration % self.print_every == 0:
                self.print_log(header=self._iteration == 0)
            self._iteration += 1
        self._epoch += 1
        self.previous_log.extend(self.log)
        self.log = []

    def test(self, *args):
        # args is X1, X2...Xn, Y
        # Where Xs are features, Y is the outcome
        self.model.train(False)
        self._iteration = 0
        self.optimizer.zero_grad()
        for batch in chunks(self.batchsize, *args, cuda=self.cuda):
            target = batch[-1]
            pred = self.model.forward(*batch[:-1])
            loss = self.model.loss(pred, target)
            scalar = sum(loss)
            scalar.backward()
            self.run_callbacks(batch, pred, train=False, loss=scalar.data[0])
            if self._iteration % self.print_every == 0:
                self.print_log(header=self._iteration == 0)
            self._iteration += 1
        self.optimizer.zero_grad()
        self._iteration = 0
        self.previous_log.extend(self.log)
        self.log = []
        self.model.train(True)

    def run_callbacks(self, batch, pred, **kwargs):
        vals = {name: cb(batch, self.model, pred)
                for (name, cb) in self.callbacks.items()}
        vals['timestamp'] = time.time()
        vals['epoch'] = self._epoch
        vals['iteration'] = self._iteration
        vals.update(kwargs)
        self.log.append(vals)

    def print_log(self, header=False):
        logs = pd.DataFrame(self.log).sort_values('timestamp')
        roll = logs.rolling(window=self.window).mean().reset_index()
        logs = logs.reset_index()
        concat = logs.merge(roll, how='left', on='index',
                            suffixes=('', '_rolling'))
        del_keys = ['iter_time_rolling', 'iteration_rolling',
                    'timestamp_rolling', 'epoch_rolling', 'train_rolling',
                    'index', 'timestamp']
        for key in del_keys:
            if key in concat.columns:
                del concat[key]
        line = (concat.tail(1)
                      .applymap("{0:1.2e}".format)
                      .to_string(header=header))
        print(line)

    def print_summary(self):
        logs = pd.DataFrame(self.previous_log).sort_values('timestamp')
        print('SUMMARY------')
        print(logs.groupby(('epoch', 'train')).mean())
        print('')
