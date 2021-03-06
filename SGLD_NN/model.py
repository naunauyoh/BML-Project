"""Model using SGLD"""

from SGLD_NN.utils import SGLD, log_gaussian_loss, to_variable, draw_train_data, draw_learned_dist, \
    draw_loss_over_iteration
import torch.nn as nn
import torch
import numpy as np
import copy
import matplotlib.pyplot as plt
import GPy
import os
import time

# torch.device = 'cuda' if torch.cuda.is_available() else 'cpu'
torch.device = 'cpu'
device = torch.device


class Langevin_Layer(nn.Module):
    """Layer whose weights and biases are close to 0"""

    def __init__(self, input_dim, output_dim):
        super(Langevin_Layer, self).__init__()
        self.input_dim = input_dim
        self.output_dim = output_dim

        self.weights = nn.Parameter(torch.Tensor(self.input_dim, self.output_dim).uniform_(-0.01, 0.01))
        self.biases = nn.Parameter(torch.Tensor(self.output_dim).uniform_(-0.01, 0.01))

    def forward(self, x):
        return torch.mm(x, self.weights) + self.biases


class Langevin_Model(nn.Module):
    """2 Layer Model based on the Langevin Layers """

    def __init__(self, input_dim, output_dim, num_units, init_log_noise):
        super(Langevin_Model, self).__init__()

        self.input_dim = input_dim
        self.output_dim = output_dim

        # network with two hidden and one output layer
        self.layer1 = Langevin_Layer(input_dim, num_units)
        self.layer2 = Langevin_Layer(num_units, 2 * output_dim)

        # activation to be used between hidden layers
        self.activation = nn.ReLU(inplace=True)
        self.log_noise = nn.Parameter(torch.FloatTensor([init_log_noise]))

    def forward(self, x):
        x = x.view(-1, self.input_dim)

        x = self.layer1(x)
        x = self.activation(x)

        x = self.layer2(x)

        return x


class Langevin_Wrapper:
    """Container for the Langevin Model that applies SGLD"""

    def __init__(self, input_dim, output_dim, no_units, learn_rates, batch_size, init_log_noise,
                 weight_decay):
        self.learn_rates = learn_rates
        self.weight_decay = weight_decay
        self.batch_size = batch_size

        self.network = Langevin_Model(input_dim=input_dim,
                                      output_dim=output_dim,
                                      num_units=no_units,
                                      init_log_noise=init_log_noise,
                                      )
        if torch.device == "cuda":
            self.network.to(device)

        self.count = 0

        self.loss_func = log_gaussian_loss

    def fit(self, x, y):
        optimizer = SGLD(self.network.parameters(), lr=self.learn_rates[self.count], weight_decay=self.weight_decay)

        x, y = to_variable(var=(x, y), cuda=torch.device == "cuda")

        # reset gradient and total loss
        optimizer.zero_grad()
        output = self.network(x)
        loss = self.loss_func(output, y, torch.exp(self.network.log_noise), 1)

        loss.backward()
        optimizer.step()

        return loss


if __name__ == "__main__":
    # Generate the data
    np.random.seed(2)
    no_points = 400
    lengthscale = 1
    variance = 1.0
    sig_noise = 0.3
    get_figs = True
    x = np.random.uniform(-3, 3, no_points)[:, None]
    x.sort(axis=0)

    k = GPy.kern.RBF(input_dim=1, variance=variance, lengthscale=lengthscale)
    C = k.K(x, x) + np.eye(no_points) * sig_noise ** 2

    y = np.random.multivariate_normal(np.zeros((no_points)), C)[:, None]
    y = (y - y.mean())
    margin = 75
    x_train = x[margin:no_points - margin]
    y_train = y[margin:no_points - margin]

    # Shuffle data
    to_shuffle = list(zip(x_train, y_train))
    np.random.shuffle(to_shuffle)
    x_train, y_train = zip(*to_shuffle)
    x_train, y_train = np.array(x_train), np.array(y_train)

    draw_train_data(x_train, y_train)

    # Define the learning environment
    best_net, best_loss = None, float('inf')
    num_nets = 100
    mix_epochs, burnin_epochs = 100, 3000
    num_epochs = mix_epochs * num_nets + burnin_epochs

    # Here we define the number of batches we want to use to train the model
    # nos_batches = [1, 2, 5, 10, 25, 50, 100]
    nos_batches = [7]

    final_scores = list()

    # Run for various batches sizes (or number of batches)
    for no_batch in nos_batches:

        print("\nUsing {} batches".format(no_batch))

        batch_size, nb_train = len(x_train) // no_batch, len(x_train)

        batches = [(x_train[i: min(i + batch_size, nb_train - 1)],
                    y_train[i:  min(i + batch_size, nb_train - 1)]) for i in range(0, nb_train, batch_size)]

        # Here we define the sequence of learning rates that we will use for training the model
        # Note that we want the sequence to go towards 0.
        l_r_lin_space = np.linspace(5e-5, 5e-5, num_epochs)

        net = Langevin_Wrapper(input_dim=1, output_dim=1, no_units=200, learn_rates=l_r_lin_space,
                               batch_size=batch_size, init_log_noise=0, weight_decay=1)

        dir_name = './SGLD_NN/fig_' + str(no_batch) + 'batch'
        if not os.path.exists(dir_name):
            os.mkdir(dir_name)

        train_losses = [[] for _ in range(no_batch)]
        secs = []
        nets = []
        record_period = 2

        start = time.time()

        for i in range(1, num_epochs + 1):

            losses = list()
            for x_batch, y_batch in batches:
                losses.append(net.fit(x_train, y_train).cpu().data.numpy())
            batch_mean_loss = np.mean(losses)
            net.count += 1

            if i % record_period == 0:
                # print('Epoch: %4d, Train loss = %8.3f' % (i, batch_mean_loss))
                for j in range(no_batch):
                    train_losses[j].append(losses[j])
                secs.append(time.time() - start)

            if i % mix_epochs == 0 and i > burnin_epochs:
                nets.append(copy.deepcopy(net.network))

            if i % 500 == 0 and i > burnin_epochs and get_figs:

                # print("Using %d networks for prediction" % len(nets))
                samples = []
                noises = []
                for network in nets:
                    if torch.device == 'cuda':
                        preds = network.forward(torch.linspace(-5, 5, 200).cuda()).cpu().data.numpy()
                    else:
                        preds = network.forward(torch.linspace(-5, 5, 200)).data.numpy()
                    samples.append(preds)
                    noises.append(torch.exp(network.log_noise).cpu().data.numpy())

                samples = np.array(samples)
                noises = np.array(noises).reshape(-1)
                means = (samples.mean(axis=0)).reshape(-1)

                aleatoric = noises.mean()  # Error coming from noise
                epistemic = (samples.var(axis=0) ** 0.5).reshape(-1)  # Error from approximation of the model
                total_unc = (aleatoric ** 2 + epistemic ** 2) ** 0.5  # Geometric averaging

                draw_learned_dist(x_train, y_train, means, aleatoric, total_unc, dir_name, i)

        draw_loss_over_iteration(train_losses, no_batch, num_epochs, dir_name, record_period)

    for no_batch, final_score in zip(nos_batches, final_scores):
        print("For {} batches we have {} at the end".format(no_batch, final_score))
