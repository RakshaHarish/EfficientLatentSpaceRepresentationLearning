import torch
import torch.nn as nn
import torch.nn.functional as tf
import numpy as np
import matplotlib.pyplot as plt

import pyro
import pyro.distributions as dist
from pyro.infer import SVI, Trace_ELBO
from pyro.optim import Adam

from nyu_dataloader import setup_data_loaders

pyro.set_rng_seed(101)

# Input is 216*216
class Encoder(nn.Module):
  def __init__(self, z_dim):
    super().__init__()

    depths = [4, 32, 64, 128, 256, 512]
    convs = []
    for i in range(0, len(depths)-1):
      convs.append(nn.Sequential(nn.Conv2d(depths[i], depths[i+1], 4, padding=1, stride=2), nn.BatchNorm2d(depths[i+1]), nn.LeakyReLU()))

    self.convs = nn.Sequential(*convs)

    self.fc1 = nn.Linear(depths[-1]*4**2, z_dim)
    self.fc2 = nn.Linear(depths[-1]*4**2, z_dim)


  def forward(self, x):
    conv_out = self.convs(x).flatten(1)

    mu = self.fc1(conv_out)
    log_var = self.fc2(conv_out)

    return mu, log_var


class Decoder(nn.Module):
  def __init__(self, z_dim):
    super().__init__()

    depths = [512, 256, 128, 64, 32, 4]

    self.fc = nn.Linear(z_dim, depths[0]*4**2)

    convs = []
    for i in range(0, len(depths)-1):
      convs.append(nn.Sequential(nn.UpsamplingNearest2d(scale_factor=2), nn.Conv2d(depths[i], depths[i+1], 3, padding=1, padding_mode='replicate'), nn.BatchNorm2d(depths[i+1]), nn.LeakyReLU()))

    self.convs = nn.Sequential(*convs)

  def forward(self, z):
    fc_out = self.fc(z).view(z.shape[0], -1, 4, 4)
    conv_out = self.convs(fc_out)
    final = torch.sigmoid(conv_out)  # 4 independent bernoulli variables per pixel

    return final


class VAE(nn.Module):
  def __init__(self, z_dim):
    super().__init__()
    self.z_dim = z_dim
    self.encoder = Encoder(self.z_dim)
    self.decoder = Decoder(self.z_dim)

    if torch.cuda.is_available():
      self.cuda()

  # p(x, z) = p(x|z)p(z)
  def model(self, x):
    pyro.module("decoder", self.decoder)
    with pyro.plate("data", x.shape[0]):
      # mean and variance of prior p(z)
      z_mu = x.new_zeros(torch.Size((x.shape[0], self.z_dim)))
      z_var = x.new_ones(torch.Size((x.shape[0], self.z_dim)))

      z = pyro.sample("latent", dist.Normal(z_mu, z_var).to_event(1))
      x_means = self.decoder(z)
      pyro.sample("obs", dist.Bernoulli(x_means).to_event(3), obs=x)

  # approximate posterior q(z|x)
  def guide(self, x):
    pyro.module("encoder", self.encoder)
    with pyro.plate("data", x.shape[0]):
      z_mu, z_log_var = self.encoder(x)
      pyro.sample("latent", dist.Normal(z_mu, torch.exp(z_log_var)).to_event(1))

  def reconstruct(self, x):
    z_mu, z_log_var = self.encoder(x)
    z = dist.Normal(z_mu, torch.exp(z_log_var)).sample()
    x = self.decoder(z)
    return x


# Trains for one epoch
def train(svi, train_loader):
    epoch_loss = 0
    for x in train_loader:
      if torch.cuda.is_available():
        x = x.cuda()

        # compute ELBO gradient and accumulate loss
        epoch_loss += svi.step(x)

    # return epoch loss
    total_epoch_loss_train = epoch_loss / len(train_loader.dataset)
    return total_epoch_loss_train


def evaluate(svi, test_loader, use_cuda=False):
    test_loss = 0
    # compute the loss over the entire test set
    for x in test_loader:
      if torch.cuda.is_available():
          x = x.cuda()

      # compute ELBO estimate and accumulate loss
      test_loss += svi.evaluate_loss(x)

    total_epoch_loss_test = test_loss / len(test_loader.dataset)
    return total_epoch_loss_test


pyro.clear_param_store()
# pyro.enable_validation(True)
# pyro.distributions.enable_validation(False)

vae = VAE(400)
optimizer = Adam({"lr": 1e-4})

# num_particles defaults to 1. Can increase to get ELBO over multiple samples of z~q(z|x).
svi = SVI(vae.model, vae.guide, optimizer, loss=Trace_ELBO())

# vae.load_state_dict(torch.load('torch_weights.save'))
# optimizer.load('optimizer_state.save')

NUM_EPOCHS = 300
TEST_FREQUENCY = 5
BATCH_SIZE = 50
train_loader, test_loader = setup_data_loaders(batch_size=BATCH_SIZE)

train_elbo = []
test_elbo = []

# vae.eval()
best = float('inf')

vae.train()

fig, axs = plt.subplots(2, 2)

for epoch in range(NUM_EPOCHS):
    total_epoch_loss_train = train(svi, train_loader)
    train_elbo.append(-total_epoch_loss_train)
    print("[epoch %d]  average training loss: %.4f" % (epoch, total_epoch_loss_train))

    if epoch % TEST_FREQUENCY == 0:
        vae.eval()
        total_epoch_loss_test = evaluate(svi, test_loader)
        vae.train()
        test_elbo.append(-total_epoch_loss_test)
        print("[epoch %d] average test loss: %.4f" % (epoch, total_epoch_loss_test))

        if total_epoch_loss_test < 0:  # numerical instability occured
          print('Negative loss occurred!!!', total_epoch_loss_test)
          break

        # Save stuff
        if total_epoch_loss_test < best:
          print('SAVING EPOCH', epoch)
          best = total_epoch_loss_test
          optimizer.save('rgbd_pvae_optimizer_state.save')
          torch.save(vae.state_dict(), 'rgbd_pvae_torch_weights.save')

        i = 0
        axs[0, 0].imshow(test_loader.dataset[i][:3].permute(1, 2, 0))
        axs[0, 1].imshow(test_loader.dataset[i][3])

        test_input = test_loader.dataset[i].unsqueeze(0).cuda()
        reconstructed = vae.reconstruct(test_input).cpu().detach()[0]
        axs[1, 0].imshow(reconstructed[:3].permute(1, 2, 0))
        axs[1, 1].imshow(reconstructed[3])
        plt.savefig('test.png')
        plt.cla()
