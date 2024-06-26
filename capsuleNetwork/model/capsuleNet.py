import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from torch.optim import Adam
from torchvision import datasets, transforms
from torchvision.datasets import MNIST
from torch.nn.utils import clip_grad_norm_
from torch.utils.data import DataLoader


USE_CUDA = False

# Define a class to manage the MNIST dataset


class Mnist:
    def __init__(self, batch_size):
        # Define transformations for the dataset
        transform = transforms.Compose([
            transforms.ToTensor(),  # Convert PIL image to PyTorch tensor
            transforms.Normalize((0.1307,), (0.3081,))  # Normalize images
        ])

        # Load the MNIST train and test sets using torchvision datasets
        train_dataset = MNIST(root='./data', train=True,
                              download=True, transform=transform)
        test_dataset = MNIST(root='./data', train=False,
                             download=True, transform=transform)

        # Create data loaders for training and testing
        self.train_loader = DataLoader(
            train_dataset, batch_size=batch_size, shuffle=True)
        self.test_loader = DataLoader(
            test_dataset, batch_size=batch_size, shuffle=False)


class CapsNet(nn.Module):
    def __init__(self):
        super(CapsNet, self).__init__()
        self.conv_layer = ConvLayer()
        self.primary_capsules = PrimaryCaps()
        self.digit_capsules = DigitCaps()
        self.decoder = Decoder()

    def forward(self, data):
        output = self.digit_capsules(
            self.primary_capsules(self.conv_layer(data)))
        reconstructions, masked = self.decoder(output, data)
        return output, reconstructions, masked

    def loss(self, data, x, target, reconstructions):
        return self.margin_loss(x, target) + self.reconstruction_loss(data, reconstructions)

    def margin_loss(self, x, labels, size_average=True):
        batch_size = x.size(0)
        v_c = torch.sqrt((x**2).sum(dim=2, keepdim=True))
        left = F.relu(0.9 - v_c).view(batch_size, -1)
        right = F.relu(v_c - 0.1).view(batch_size, -1)
        loss = labels * left + 0.5 * (1.0 - labels) * right
        loss = loss.sum(dim=1).mean()
        return loss

    def reconstruction_loss(self, data, reconstructions):
        # Flatten the reconstruction tensor
        recon_flat = reconstructions.view(reconstructions.size(0), -1)
        # Flatten and normalize the input data tensor to obtain class probabilities
        # Convert to FloatTensor
        data_flat = data.view(data.size(0), -1).float()
        data_prob = F.softmax(data_flat, dim=1)
        # Calculate the reconstruction loss
        recon_loss = F.binary_cross_entropy_with_logits(
            recon_flat, data_prob, reduction='mean')
        return recon_loss


class ConvLayer(nn.Module):
    def __init__(self, in_channels=1, out_channels=256, kernel_size=9):
        super(ConvLayer, self).__init__()
        self.conv = nn.Conv2d(
            in_channels=in_channels, out_channels=out_channels, kernel_size=kernel_size, stride=1)

    def forward(self, x):
        return F.relu(self.conv(x))


class PrimaryCaps(nn.Module):
    def __init__(self, num_capsules=8, in_channels=256, out_channels=32, kernel_size=9):
        super(PrimaryCaps, self).__init__()
        self.capsules = nn.ModuleList([
            nn.Conv2d(in_channels=in_channels, out_channels=out_channels,
                      kernel_size=kernel_size, stride=2, padding=0)
            for _ in range(num_capsules)])

    def forward(self, x):
        u = [capsule(x) for capsule in self.capsules]
        u = torch.stack(u, dim=1)
        u = u.view(x.size(0), 32 * 6 * 6, -1)
        return self.squash(u)

    def squash(self, input_tensor):
        squared_norm = torch.norm(input_tensor, p=2, dim=-1, keepdim=True) ** 2
        output_tensor = squared_norm * input_tensor / \
            ((1. + squared_norm) * torch.sqrt(squared_norm))
        return output_tensor


class DigitCaps(nn.Module):
    def __init__(self, num_capsules=10, num_routes=32 * 6 * 6, in_channels=8, out_channels=16):
        super(DigitCaps, self).__init__()
        self.in_channels = in_channels
        self.num_routes = num_routes
        self.num_capsules = num_capsules
        self.W = nn.Parameter(torch.randn(
            1, num_routes, num_capsules, out_channels, in_channels))

    def forward(self, x):
        batch_size = x.size(0)
        x = torch.stack([x] * self.num_capsules, dim=2).unsqueeze(4)
        W = torch.cat([self.W] * batch_size, dim=0)
        u_hat = torch.matmul(W, x)
        b_ij = Variable(torch.zeros(1, self.num_routes, self.num_capsules, 1))
        if USE_CUDA:
            b_ij = b_ij.cuda()
        num_iterations = 3
        for iteration in range(num_iterations):
            c_ij = F.softmax(b_ij, dim=1)
            c_ij = torch.cat([c_ij] * batch_size, dim=0).unsqueeze(4)
            s_j = (c_ij * u_hat).sum(dim=1, keepdim=True)
            v_j = self.squash(s_j)
            if iteration < num_iterations - 1:
                a_ij = torch.matmul(u_hat.transpose(
                    3, 4), torch.cat([v_j] * self.num_routes, dim=1))
                b_ij = b_ij + a_ij.squeeze(4).mean(dim=0, keepdim=True)
        return v_j.squeeze(1)

    def squash(self, input_tensor):
        squared_norm = torch.norm(input_tensor, p=2, dim=-1, keepdim=True) ** 2
        output_tensor = squared_norm * input_tensor / \
            ((1. + squared_norm) * torch.sqrt(squared_norm))
        return output_tensor


class Decoder(nn.Module):
    def __init__(self):
        super(Decoder, self).__init__()

        self.reconstraction_layers = nn.Sequential(
            nn.Linear(16 * 10, 512),
            nn.ReLU(inplace=True),
            nn.Linear(512, 1024),
            nn.ReLU(inplace=True),
            nn.Linear(1024, 784),
            nn.Sigmoid()
        )

    def forward(self, x, data):
        classes = torch.sqrt((x ** 2).sum(2))
        classes = F.softmax(classes, dim=1)

        _, max_length_indices = classes.max(dim=1)
        masked = Variable(torch.sparse.torch.eye(10))
        if USE_CUDA:
            masked = masked.cuda()
        masked = masked.index_select(
            dim=0, index=max_length_indices.squeeze(1).data)

        reconstructions = self.reconstraction_layers(
            (x * masked[:, :, None, None]).view(x.size(0), -1))
        reconstructions = reconstructions.view(-1, 1, 28, 28)

        return reconstructions, masked
