import datetime
from typing import List

from torch.distributed import rpc
import logging
import numpy as np

from fltk.client import Client
from fltk.util.weight_init import *
from fltk.nets.md_gan import *

from fltk.util.results import EpochData, GANEpochData


def _call_method(method, rref, *args, **kwargs):
    """helper for _remote_method()"""
    return method(rref.local_value(), *args, **kwargs)


def _remote_method_async(method, rref, *args, **kwargs):
    args = [method, rref] + list(args)
    return rpc.rpc_async(rref.owner(), _call_method, args=args, kwargs=kwargs)


class ClientMDGAN(Client):
    counter = 0
    finished_init = False
    dataset = None
    epoch_results: List[EpochData] = []
    epoch_counter = 0

    def __init__(self, id, log_rref, rank, world_size, config=None, batch_size=20):
        super().__init__(id, log_rref, rank, world_size, config)
        logging.info(f'Welcome to MD client {id}')
        self.latent_dim = 10
        self.batch_size = batch_size
        self.discriminator = Discriminator(32)
        self.discriminator.apply(weights_init_normal)
        self.E = 5
        self.adversarial_loss = torch.nn.MSELoss()

    def init_dataloader(self, ):
        self.args.distributed = True
        self.args.rank = self.rank
        self.args.world_size = self.world_size
        self.dataset = self.args.DistDatasets[self.args.dataset_name](self.args)
        self.imgs = self.dataset.load_train_dataset()
        self.finished_init = True

        self.batch_size = self.args.DistDatasets[self.args.batch_size](self.args)

        logging.info('Done with init')

    def set_batch_size(self, batch_size):
        self.batch_size = batch_size

    def get_client_list(self, client_list):
        self.client_list = client_list
        for idx in range(len(self.client_list)):
            if self.client_list[idx][1] == self.id:
                self.client_idx = idx
                return

    def train_md(self, epoch, Xd):
        """
        :param epoch: Current epoch #
        :type epoch: int
        """
        # save model
        if self.args.should_save_model(epoch):
            self.save_model(epoch, self.args.get_epoch_save_start_suffix())

        if self.args.distributed:
            self.dataset.train_sampler.set_epoch(epoch)

        inputs = self.dataset.load_train_dataset()[0]
        rnd_indices = np.random.choice(len(inputs), size=self.batch_size)
        inputs, labels, fake = torch.from_numpy(inputs[rnd_indices]), \
                               torch.ones(self.batch_size, dtype=torch.float), \
                               torch.ones(self.batch_size, dtype=torch.float)

        # zero the parameter gradients
        self.optimizer.zero_grad()

        # outputs = self.discriminator(inputs)

        # not sure about loss function - shall we use A_hat / B_hat?
        # fake_loss = self.adversarial_loss(self.discriminator(Xd.detach()), fake)
        # real_loss = self.adversarial_loss(outputs, labels)
        fake_loss = self.B_hat(Xd)
        real_loss = self.A_hat(inputs)

        d_loss = (real_loss + fake_loss)
        d_loss.backward()
        self.optimizer.step()

        # save model
        if self.args.should_save_model(epoch):
            self.save_model(epoch, self.args.get_epoch_save_end_suffix())

    def A_hat(self, Xr):
        return (1 / self.batch_size) * sum(torch.log(self.discriminator(Xr)))

    def B_hat(self, Xg):
        return (1 / self.batch_size) * sum(torch.log(1 - self.discriminator(Xg)))

    def calculate_error(self, Xg):
        b_hat = self.B_hat(Xg)
        b_hat.backward(retain_graph=True)
        print(type(Xg.grad))
        return Xg.grad

    def get_new_discriminator(self, discriminator):
        self.discriminator = discriminator

    def swap_discriminator(self, epoch):
        client_id = (self.client_idx + epoch) % len(self.client_list)
        while client_id == self.client_idx:
            client_id = (client_id + 1) % len(self.client_list)
        response = _remote_method_async(ClientMDGAN.get_new_discriminator, self.client_list[client_id][0],
                                        discriminator=self.discriminator)
        response.wait()

    def run_epochs(self, num_epoch, current_epoch=1, Xs=None):
        start_time_train = datetime.datetime.now()
        Xd, Xg = Xs

        for e in range(num_epoch):
            self.train_md(self.epoch_counter, Xd)
            self.epoch_counter += 1

        error = self.calculate_error(Xg)
        elapsed_time_train = datetime.datetime.now() - start_time_train
        train_time_ms = int(elapsed_time_train.total_seconds()*1000)

        start_time_test = datetime.datetime.now()
        elapsed_time_test = datetime.datetime.now() - start_time_test
        test_time_ms = int(elapsed_time_test.total_seconds()*1000)

        data = GANEpochData(self.epoch_counter, train_time_ms, test_time_ms, error, client_id=self.id)
        self.epoch_results.append(data)

        return data
