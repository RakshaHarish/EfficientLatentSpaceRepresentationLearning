import torch
import torch.nn as nn
import torch.nn.functional as tf
import numpy as np

import pyro
import pyro.distributions as dist
from pyro.infer import SVI, Trace_ELBO
from pyro.optim import Adam


# TODO REMOVE AFTER TESTING!!!!! MAKES STUFF SLOW
pyro.enable_validation(True)
pyro.distributions.enable_validation(False)


# Deep Residual Learning for Image Recognition: https://arxiv.org/pdf/1512.03385.pdf
class ResBlock(nn.Module):
  def __init__(self, in_channels, out_channels):
    super().__init__()

    self.conv = nn.Sequential(nn.Conv2d(in_channels, out_channels, 3, padding=1), nn.InstanceNorm2d(out_channels), nn.ReLU(),
                              nn.Conv2d(in_channels, out_channels, 3, padding=1), nn.InstanceNorm2d(out_channels))
    
    self.linear = None
    if in_channels != out_channels:
      self.linear = nn.Sequential(nn.Conv2d(in_channels, out_channels, 1, padding=0), nn.InstanceNorm2d(out_channels))

  def forward(self, x):
    if self.linear:
      x = self.linear(x)
    return tf.relu(self.conv(x) + x)

# Input is 216*216
class Encoder(nn.Module):
  def __init__(self):
    super().__init__()
    
    depths = [64, 76, 88, 100, 128, 200]
    self.conv1 = nn.Sequential(nn.Conv2d(4, 64, 3, padding=1), nn.InstanceNorm2d(64), nn.LeakyReLU())

    convs = []
    for i in range(0, len(depths)-1):
      convs.append(nn.Sequential(nn.Conv2d(depths[i], depths[i+1], 3, padding=1, stride=2), nn.InstanceNorm2d(depths[i+1]), nn.ReLU()))

    self.convs = nn.Sequential(*convs)
    self.res_blocks = nn.Sequential(ResBlock(depths[-1], depths[-1]), ResBlock(depths[-1], depths[-1]))

    self.conv2 = nn.Sequential(nn.Conv2d(200, 250, 3, padding=1, stride=2), nn.InstanceNorm2d(250), nn.ReLU())
    self.conv3 = nn.Sequential(nn.Conv2d(250, 300, 3, padding=1, stride=2), nn.InstanceNorm2d(300), nn.ReLU())
    self.fc = nn.Linear(4, 2)

  def forward(self, x):
    conv1_out = self.conv1(x)
    conv_out = self.convs(conv1_out)
    res_out = self.res_blocks(conv_out)
    conv2_out = self.conv2(res_out)
    conv3_out = self.conv3(conv2_out)  

    final = conv3_out.flatten(2)
    final = self.fc(final)  # mu, log(var)
    
    return final

class Decoder(nn.Module):
  def __init__(self):
    super().__init__()

    self.conv1 = nn.Sequential(nn.ConvTranspose2d(300, 250, 4), nn.InstanceNorm2d(250), nn.ReLU())
    self.conv2 = nn.Sequential(nn.ConvTranspose2d(250, 200, 3), nn.InstanceNorm2d(200), nn.ReLU())

    depths = [200, 128, 100, 88, 76, 64]
    self.res_blocks = nn.Sequential(ResBlock(depths[0], depths[0]), ResBlock(depths[0], depths[0]))

    convs = []
    for i in range(0, 2):
      convs.append(nn.Sequential(nn.ConvTranspose2d(depths[i], depths[i+1], 3, stride=2), nn.InstanceNorm2d(depths[i+1]), nn.ReLU()))

    for i in range(2, len(depths)-1):
      convs.append(nn.Sequential(nn.ConvTranspose2d(depths[i], depths[i+1], 2, stride=2), nn.InstanceNorm2d(depths[i+1]), nn.ReLU()))

    self.convs = nn.Sequential(*convs)
    self.conv3 = nn.Sequential(nn.Conv2d(64, 4, 3, padding=1), nn.InstanceNorm2d(4), nn.LeakyReLU())

  def forward(self, z):
    z = z.view(z.shape[0], z.shape[1], 1, 1)
    conv1_out = self.conv1(z)
    conv2_out = self.conv2(conv1_out)
    res_out = self.res_blocks(conv2_out)
    convs_out = self.convs(res_out)
    conv3_out = self.conv3(convs_out)
    final = torch.sigmoid(conv3_out)  # 4 independent bernoulli variables per pixel

    return final

class VAE(nn.Module):
  def __init__(self):
    super().__init__()
    self.encoder = Encoder()
    self.decoder = Decoder()

    if torch.cuda.is_available():
      self.cuda()

    self.z_dim = 300
  
  # p(x, z) = p(x|z)p(z)
  def model(self, x):
    pyro.module("decoder", self.decoder)
    with pyro.plate("data", x.shape[0]):
      # mean and variance of prior p(z)
      z_mu = x.new_zeros(torch.Size((x.shape[0], self.z_dim)))
      z_var = x.new_ones(torch.Size((x.shape[0], self.z_dim)))

      z = pyro.sample("latent", dist.Normal(z_mu, z_var).to_event(1))
      x = self.decoder(z)
      pyro.sample("obs", dist.Bernoulli(x).to_event(3), obs=x)

  # approximate posterior q(z|x)
  def guide(self, x):
    pyro.module("encoder", self.encoder)
    with pyro.plate("data", x.shape[0]):
      params = self.encoder(x)
      z_mu = params[:, :, 0]
      z_log_var = params[:, :, 1]
      pyro.sample("latent", dist.Normal(z_mu, torch.exp(z_log_var)).to_event(1))

# Trains for one epoch
def train(svi, train_loader):
    epoch_loss = 0
    for x, _ in train_loader:
      if torch.cuda.is_availabe():
        x = x.cuda()
        
        # compute ELBO gradient and accumulate loss
        epoch_loss += svi.step(x)

    # return epoch loss
    total_epoch_loss_train = epoch_loss / len(train_loader.dataset)
    return total_epoch_loss_train

def evaluate(svi, test_loader, use_cuda=False):
    test_loss = 0
    # compute the loss over the entire test set
    for x, _ in test_loader:
      if torch.cuda.is_availabe():
          x = x.cuda()

      # compute ELBO estimate and accumulate loss
      test_loss += svi.evaluate_loss(x)

    total_epoch_loss_test = test_loss / len(test_loader.dataset)
    return total_epoch_loss_test


pyro.clear_param_store()

vae = VAE()
optimizer = Adam({"lr": 1.0e-3})

# num_particles defaults to 1. Can increase to get ELBO over multiple samples of z~q(z|x).
svi = SVI(vae.model, vae.guide, optimizer, loss=Trace_ELBO())

NUM_EPOCHS = 50
TEST_FREQUENCY = 5
train_loader, test_loader = None, None  # TODO

train_elbo = []
test_elbo = []

vae.train()

for epoch in range(NUM_EPOCHS):
    total_epoch_loss_train = train(svi, train_loader)
    train_elbo.append(-total_epoch_loss_train)
    print("[epoch %03d]  average training loss: %.4f" % (epoch, total_epoch_loss_train))

    if epoch % TEST_FREQUENCY == 0:
        vae.eval()
        total_epoch_loss_test = evaluate(svi, test_loader)
        vae.train()
        test_elbo.append(-total_epoch_loss_test)
        print("[epoch %03d] average test loss: %.4f" % (epoch, total_epoch_loss_test))


# TODO can graph train_elbo and test_elbo
