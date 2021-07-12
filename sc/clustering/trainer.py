import itertools

import torch
from torch import nn
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
import torch.optim as optim
import torch_optimizer as ex_optim
import shutil
import os
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.tensorboard import SummaryWriter
from sc.clustering.model import CompactDecoder, CompactEncoder, Encoder, Decoder, GaussianSmoothing, DummyDualAAE, DiscriminatorCNN, DiscriminatorFC
from sc.clustering.dataloader import get_dataloaders
from sklearn.metrics import f1_score, confusion_matrix
from torchvision import transforms
from sc.clustering.dataloader import CoordNumSpectraDataset, ToTensor
from scipy.stats import shapiro, spearmanr


class Trainer:

    def __init__(self, encoder, decoder, discriminator, device, train_loader, val_loader,
                 val_sampling_weights_per_cn, base_lr=0.0001, nstyle=2,
                 n_coord_num=3, batch_size=111, max_epoch=300,
                 tb_logdir="runs", zero_conc_thresh=0.05, use_cnn_dis=False,
                 grad_rev_beta=1.1, alpha_flat_step=100, alpha_limit=2.0,
                 sch_factor=0.25, sch_patience=300, spec_noise=0.01, weight_decay=1e-2,
                 lr_ratio_Reconn=2.0, lr_ratio_Mutual=3.0, lr_ratio_Smooth=0.1,
                 lr_ratio_Supervise=2.0, lr_ratio_Style=0.5, optimizer_name="AdamW",
                 chem_dict=None, verbose=True, work_dir='.',
                 use_flex_spec_target=False, short_circuit_cn=True):

        self.encoder = encoder.to(device)
        self.decoder = decoder.to(device)
        self.discriminator = discriminator.to(device)

        self.nclasses = n_coord_num
        self.nstyle = nstyle

        self.val_weights_per_cn = torch.tensor(val_sampling_weights_per_cn, dtype=torch.float, device=device)
        self.batch_size = batch_size
        self.max_epoch = max_epoch
        self.base_lr = base_lr
        self.device = device
        self.train_loader = train_loader
        self.val_loader = val_loader

        self.noise_test_range = (-2, 2)
        self.ntest_per_spectra = 10
        self.zero_conc_thresh = zero_conc_thresh
        gau_kernel_size = 17
        self.gaussian_smoothing = GaussianSmoothing(channels=1, kernel_size=gau_kernel_size, sigma=3.0, dim=1,
                                                    device=device).to(device)
        self.padding4smooth = nn.ReplicationPad1d(padding=(gau_kernel_size - 1) // 2).to(device)

        if verbose:
            self.tb_writer = SummaryWriter(log_dir=os.path.join(work_dir, tb_logdir))
            example_spec = iter(train_loader).next()[0]
            self.tb_writer.add_graph(DummyDualAAE(use_cnn_dis, self.encoder.__class__, self.decoder.__class__), example_spec)

        self.grad_rev_beta = grad_rev_beta
        self.alpha_flat_step = alpha_flat_step
        self.alpha_limit = alpha_limit
        self.sch_factor = sch_factor
        self.sch_patience = sch_patience
        self.spec_noise = spec_noise
        self.lr_ratio_Reconn = lr_ratio_Reconn
        self.lr_ratio_Mutual = lr_ratio_Mutual
        self.lr_ratio_Smooth = lr_ratio_Smooth
        self.lr_ratio_Supervise = lr_ratio_Supervise
        self.lr_ratio_Style = lr_ratio_Style
        self.n_coord_num = n_coord_num
        self.chem_dict = chem_dict
        self.verbose = verbose
        self.work_dir = work_dir
        self.use_flex_spec_target = use_flex_spec_target
        self.short_circuit_cn = short_circuit_cn
        self.optimizer_name = optimizer_name
        self.weight_decay = weight_decay

    def sample_categorical(self):
        """
         Sample from a categorical distribution
         of size batch_size and # of classes n_classes
         return: torch.autograd.Variable with the sample
        """
        idx = np.random.randint(0, self.nclasses, self.batch_size)
        cat = np.eye(self.nclasses)[idx].astype('float32')
        cat = torch.tensor(cat, requires_grad=False, device=self.device)
        return cat, idx

    def zerograd(self):
        self.encoder.zero_grad()
        self.decoder.zero_grad()
        self.discriminator.zero_grad()

    @staticmethod
    def d_entropy1(y):
        y1 = y.mean(dim=0)
        y2 = torch.sum(-y1 * torch.log(y1 + 1e-5))
        return y2

    def d_entropy2(self, y):
        y1 = -y * torch.log(y + 1e-5)
        y2 = torch.sum(y1) / self.batch_size
        return y2

    @staticmethod
    def get_cluster_idx(Y_pred):
        return Y_pred.argmax(dim=1).cpu()

    def get_cluster_plot(self, spec_list):
        assert spec_list.shape[0] == self.nclasses
        assert spec_list.shape[1] == self.ntest_per_spectra
        # noinspection PyTypeChecker
        fig, ax_list = plt.subplots(self.nclasses, 1, sharex=True, sharey=True, figsize=(9, 12))
        colors = sns.color_palette("husl", self.ntest_per_spectra)
        for i, (sl, ax) in enumerate(zip(spec_list, ax_list.ravel())):
            for spec, color in zip(sl, colors):
                ax.plot(spec, lw=1.5, c=color)
                ax.set_ylabel(f"{i + 4} Folds Coordinated")
        title = "All Classes and Styles"
        fig.suptitle(title, y=0.91)
        return fig

    def get_style_distribution_plot(self, z):
        # noinspection PyTypeChecker
        fig, ax_list = plt.subplots(self.nstyle, 1, sharex=True, sharey=True, figsize=(9, 12))
        for istyle, ax in zip(range(self.nstyle), ax_list):
            sns.distplot(z[:, istyle], kde=False, color='blue', bins=np.arange(-3.0, 3.01, 0.2),
                         ax=ax)
        return fig

    def compute_first_two_style_bvs_spearman(self, styles_np, iclasses_np):
        styles_np = styles_np.T
        val_ds = self.val_loader.dataset
        if self.chem_dict is not None and "val_bvs" not in self.chem_dict:
            keys_bvs_val = sorted(set(self.chem_dict['bvs'].keys()) & set(val_ds.atom_index))
            keys_bvs_val = list(filter(lambda k: self.chem_dict['bvs'][k] > 1.0E-5, keys_bvs_val))
            self.chem_dict['val_bvs_idx'] = np.array([ai in keys_bvs_val for ai in val_ds.atom_index])
            self.chem_dict['val_bvs'] = np.array([
                self.chem_dict['bvs'][tuple(ai)]
                for ai in np.array(val_ds.atom_index)[self.chem_dict['val_bvs_idx']].tolist()])
        styles_with_bvs = styles_np[self.chem_dict['val_bvs_idx']]
        iclasses_with_bvs = iclasses_np[self.chem_dict['val_bvs_idx']]

        bvs_mean_per_iclass = np.array([self.chem_dict['val_bvs'][iclasses_with_bvs == i].mean()
                                        if (iclasses_with_bvs == i).any() else 0.0
                                        for i in range(self.nclasses)])
        bvs_std_per_iclass = np.array([self.chem_dict['val_bvs'][iclasses_with_bvs == i].std()
                                       if (iclasses_with_bvs == i).any() else 1.0
                                       for i in range(self.nclasses)])
        styles_mean_per_iclass = np.stack([styles_with_bvs[iclasses_with_bvs == i].mean(axis=0)
                                           if (iclasses_with_bvs == i).any() else np.zeros(self.nstyle)
                                           for i in range(self.nclasses)])
        styles_std_per_iclass = np.stack([styles_with_bvs[iclasses_with_bvs == i].std(axis=0)
                                          if (iclasses_with_bvs == i).any() else np.ones(self.nstyle)
                                          for i in range(self.nclasses)])
        bvs_std_per_iclass[bvs_std_per_iclass < 0.01] = 0.01
        styles_std_per_iclass[bvs_std_per_iclass < 0.01] = 0.01
        cor_sign_per_iclass = np.sign(
            [[spearmanr(styles_with_bvs[iclasses_with_bvs == ic, iy],
                        self.chem_dict['val_bvs'][iclasses_with_bvs == ic]).correlation
              for iy in range(self.nstyle)]
             for ic in range(self.nclasses)])
        cor_sign_per_iclass[np.isnan(cor_sign_per_iclass)] = 1.0
        cor_sign_per_iclass[np.fabs(cor_sign_per_iclass) < 0.1] = 1.0
        normed_val_bvs = (self.chem_dict['val_bvs'] - bvs_mean_per_iclass[iclasses_with_bvs]) / \
            bvs_std_per_iclass[iclasses_with_bvs]
        normed_val_bvs = (cor_sign_per_iclass[iclasses_with_bvs].T * normed_val_bvs).T
        centered_style_val_for_bvs = (styles_with_bvs - styles_mean_per_iclass[iclasses_with_bvs]) / \
            styles_std_per_iclass[iclasses_with_bvs]
        sm = [spearmanr(centered_style_val_for_bvs[:, i], normed_val_bvs[:, i]).correlation
              for i in range(self.nstyle)]
        max_cor, sec_cor = np.sort(np.fabs(sm))[::-1][:2]
        return max_cor, sec_cor

    def train(self, callback=None):
        if self.verbose:
            para_info = torch.__config__.parallel_info()
            print(para_info)

        opt_cls_dict = {"Adam": optim.Adam, "AdamW": optim.AdamW,
                        "AdaBound": ex_optim.AdaBound, "RAdam": ex_optim.RAdam}
        opt_cls = opt_cls_dict[self.optimizer_name]

        # loss function
        mse_dis = nn.MSELoss().to(self.device)
        nll_loss = nn.NLLLoss().to(self.device)

        reconn_solver = opt_cls([{'params': self.encoder.parameters()}, {'params': self.decoder.parameters()}],
                                lr=self.lr_ratio_Reconn * self.base_lr,
                                weight_decay=self.weight_decay)
        mutual_info_solver = opt_cls([{'params': self.encoder.parameters()}, {'params': self.decoder.parameters()}],
                                     lr=self.lr_ratio_Mutual * self.base_lr)
        smooth_solver = opt_cls([{'params': self.decoder.parameters()}], lr=self.lr_ratio_Smooth * self.base_lr,
                                weight_decay=self.weight_decay)
        cn_solver = opt_cls([{'params': self.encoder.parameters()}], lr=self.lr_ratio_Supervise * self.base_lr,
                            weight_decay=self.weight_decay)
        adversarial_solver = opt_cls([{'params': self.discriminator.parameters()},
                                      {'params': self.encoder.parameters()}],
                                     lr=self.lr_ratio_Style * self.base_lr,
                                     betas=(self.grad_rev_beta * 0.9, self.grad_rev_beta * 0.009 + 0.99))

        sol_list = [reconn_solver, mutual_info_solver, smooth_solver, cn_solver, adversarial_solver]
        schedulers = [
            ReduceLROnPlateau(sol, mode="max", factor=self.sch_factor, patience=self.sch_patience, cooldown=0, threshold=0.01, 
                              verbose=self.verbose)
            for sol in sol_list]

        # fixed random variables, for plot spectra
        idx = np.arange(self.nclasses).repeat(self.ntest_per_spectra)
        one_hot = np.zeros((self.nclasses * self.ntest_per_spectra, self.nclasses))
        one_hot[range(self.nclasses * self.ntest_per_spectra), idx] = 1

        c = np.linspace(*self.noise_test_range, self.ntest_per_spectra).reshape(1, -1)
        c = np.repeat(c, self.nclasses, 0).reshape(-1, 1)
        c2 = np.hstack([np.zeros_like(c)] * (self.nstyle - 1) + [c])

        dis_c = torch.tensor(one_hot, dtype=torch.float, device=self.device, requires_grad=False)
        con_c = torch.tensor(c2, dtype=torch.float, device=self.device, requires_grad=False)

        # train network
        last_best = 0.0
        chkpt_dir = f"{self.work_dir}/checkpoints"
        if not os.path.exists(chkpt_dir):
            os.makedirs(chkpt_dir, exist_ok=True)
        best_chk = None
        metrics = None
        for epoch in range(self.max_epoch):
            # Set the networks in train mode (apply dropout when needed)
            self.encoder.train()
            self.decoder.train()
            self.discriminator.train()

            alpha = (2. / (1. + np.exp(-1.0E4 / self.alpha_flat_step * epoch / self.max_epoch)) - 1) * self.alpha_limit

            # Loop through the labeled and unlabeled dataset getting one batch of samples from each
            # The batch size has to be a divisor of the size of the dataset or it will return
            # invalid samples
            n_batch = len(self.train_loader)
            avg_mutual_info = 0.0
            for spec_in, cn_in in self.train_loader:
                spec_in = spec_in.to(self.device)
                cn_in = cn_in.to(self.device)
                spec_target = spec_in.clone()
                spec_in = spec_in + torch.randn_like(spec_in, requires_grad=False) * self.spec_noise

                # Init gradients, adversarial loss
                self.zerograd()
                z_real_gauss = torch.randn(self.batch_size, self.nstyle, requires_grad=True, device=self.device)
                z_fake_gauss, _ = self.encoder(spec_in)
                real_gauss_label = torch.ones(self.batch_size, dtype=torch.long, requires_grad=False,
                                              device=self.device)
                real_gauss_pred = self.discriminator(z_real_gauss, alpha)
                fake_guass_lable = torch.zeros(spec_in.size()[0], dtype=torch.long, requires_grad=False,
                                               device=self.device)
                fake_gauss_pred = self.discriminator(z_fake_gauss, alpha)
                adversarial_loss = nll_loss(real_gauss_pred, real_gauss_label) + \
                    nll_loss(fake_gauss_pred, fake_guass_lable)
                adversarial_loss.backward()
                adversarial_solver.step()

                # Init gradients, coordination number loss
                self.zerograd()
                _, y = self.encoder(spec_in)
                i_cn_in = cn_in.argmax(dim=1)
                cn_loss = nll_loss(y, i_cn_in)
                cn_loss.backward()
                cn_solver.step()

                # Init gradients, reconstruction loss
                self.zerograd()
                z, y = self.encoder(spec_in)
                if self.short_circuit_cn:
                    y = cn_in.clone()
                else:
                    y = torch.exp(y)
                spec_re = self.decoder(z, y)
                if not self.use_flex_spec_target:
                    recon_loss = mse_dis(spec_re, spec_target)
                else:
                    spec_scale = torch.abs(spec_re.mean(dim=1)) / torch.abs(spec_target.mean(dim=1))
                    recon_loss = ((spec_scale - 1.0) ** 2).mean() * 0.1
                    spec_scale = spec_scale.detach()
                    spec_scale = torch.clamp(spec_scale, min=0.7, max=1.3)
                    recon_loss += mse_dis(spec_re, (spec_target.T * spec_scale).T)
                recon_loss.backward()
                reconn_solver.step()

                # Init gradients, mutual information loss
                self.zerograd()
                y, idx = self.sample_categorical()
                z = torch.randn(self.batch_size, self.nstyle, requires_grad=False, device=self.device)
                target = torch.tensor(idx, dtype=torch.long, requires_grad=False, device=self.device)
                x_sample = self.decoder(z, y)
                z_recon, y_recon = self.encoder(x_sample)
                mutual_info_loss = nll_loss(y_recon, target) + mse_dis(z_recon, z)
                mutual_info_loss.backward()
                mutual_info_solver.step()
                avg_mutual_info += mutual_info_loss.item()

                # Init gradients, smoothness loss
                self.zerograd()
                x_sample = self.decoder(z, y)
                x_sample_padded = self.padding4smooth(x_sample.unsqueeze(dim=1))
                spec_smoothed = self.gaussian_smoothing(x_sample_padded).squeeze(dim=1)
                Smooth_loss = mse_dis(x_sample, spec_smoothed)
                Smooth_loss.backward()
                smooth_solver.step()

                # Init gradients
                self.zerograd()

                if self.verbose:
                    # record losses
                    loss_dict = {
                        'Recon': recon_loss.item(),
                        'Mutual Info': mutual_info_loss.item(),
                        'Smooth': Smooth_loss.item()
                    }
                    self.tb_writer.add_scalars("Recon/train", loss_dict, global_step=epoch)
                    loss_dict = {
                        'Classification': cn_loss.item()
                    }
                    self.tb_writer.add_scalars("Supervise/train", loss_dict, global_step=epoch)
                    loss_dict = {
                        'Adversarial': adversarial_loss.item()
                    }
                    self.tb_writer.add_scalars("Adversarial/train", loss_dict, global_step=epoch)

            avg_mutual_info /= n_batch
            self.encoder.eval()
            self.decoder.eval()
            self.discriminator.eval()
            spec_in, cn_in = [torch.cat(x, dim=0) for x in zip(*list(self.val_loader))]
            spec_in = spec_in.to(self.device)
            cn_in = cn_in.to(self.device)
            z, y = self.encoder(spec_in)
            y = torch.exp(y)
            spec_re = self.decoder(z, y)
            tw = cn_in @ self.val_weights_per_cn
            tw /= tw.sum()
            spec_diff = ((spec_re - spec_in) ** 2).mean(dim=1)
            recon_loss = (spec_diff * tw).sum()
            loss_dict = {
                'Recon': recon_loss.item()
            }
            if self.verbose:
                self.tb_writer.add_scalars("Recon/val", loss_dict, global_step=epoch)

            style_np = z.detach().clone().cpu().numpy().T
            style_shapiro = [shapiro(x).statistic for x in style_np]

            style_coupling = np.max(np.fabs([spearmanr(style_np[j1], style_np[j2]).correlation
                                             for j1, j2 in itertools.combinations(range(style_np.shape[0]), 2)]))

            class_probs = y.detach().cpu().numpy()
            iclasses = class_probs.argmax(axis=-1)
            class_pred = iclasses
            class_true = np.fabs(cn_in.detach().cpu().numpy()).argmax(axis=-1)
            valid_cn_selector = (cn_in.detach().cpu().numpy() >= 0).all(axis=1)
            class_pred = class_pred[valid_cn_selector]
            class_true = class_true[valid_cn_selector]
            cn_accuracy = f1_score(class_true, class_pred, average='weighted')

            loss_dict = {
                'F1 Score': cn_accuracy
            }
            if self.verbose:
                self.tb_writer.add_scalars("F1 Score/val", loss_dict, global_step=epoch)

            max_style_bvs_cor, sec_style_bvs_cor = None, None
            if self.chem_dict is not None:
                max_style_bvs_cor, sec_style_bvs_cor = self.compute_first_two_style_bvs_spearman(style_np, iclasses)
                loss_dict = {
                    'Max': max_style_bvs_cor,
                    'Second': sec_style_bvs_cor
                }
                if self.verbose:
                    self.tb_writer.add_scalars("Style-BVS Correlation/val", loss_dict, global_step=epoch)

            i_cn_in = cn_in.argmax(dim=1)
            cn_loss = nll_loss(y, i_cn_in)
            loss_dict = {
                'Classification': cn_loss.item()
            }
            if self.verbose:
                self.tb_writer.add_scalars("Supervise/val", loss_dict, global_step=epoch)

            z_fake_gauss = z
            z_real_gauss = torch.randn_like(z, requires_grad=True, device=self.device)
            real_gauss_label = torch.ones(spec_in.size()[0], dtype=torch.long, requires_grad=False, device=self.device)
            real_gauss_pred = self.discriminator(z_real_gauss, alpha)
            fake_guass_lable = torch.zeros(spec_in.size()[0], dtype=torch.long, requires_grad=False, device=self.device)
            fake_gauss_pred = self.discriminator(z_fake_gauss, alpha)

            adversarial_loss = nll_loss(real_gauss_pred, real_gauss_label) + nll_loss(fake_gauss_pred, fake_guass_lable)
            loss_dict = {
                'Adversarial': adversarial_loss.item()
            }
            if self.verbose:
                self.tb_writer.add_scalars("Adversarial/val", loss_dict, global_step=epoch)

            model_dict = {"Encoder": self.encoder,
                          "Decoder": self.decoder,
                          "Style Discriminator": self.discriminator}
            if cn_accuracy > last_best * 1.01:
                chk_fn = f"{chkpt_dir}/epoch_{epoch:06d}_loss_{cn_accuracy:05.4g}.pt"
                torch.save(model_dict,
                           chk_fn)
                last_best = cn_accuracy
                best_chk = chk_fn
            metrics = [cn_accuracy, min(style_shapiro), recon_loss.item(), 0.0, avg_mutual_info, style_coupling]

            for sch in schedulers:
                sch.step(last_best)

            if self.chem_dict is not None:
                metrics = metrics + [max_style_bvs_cor, sec_style_bvs_cor]
            if callback is not None:
                callback(epoch, metrics)
            # plot images
            if epoch % 25 == 0:
                spec_out = self.decoder(con_c, dis_c).reshape(self.nclasses, self.ntest_per_spectra, -1).\
                    clone().cpu().detach().numpy()
                if self.verbose:
                    fig = self.get_cluster_plot(spec_out)
                    self.tb_writer.add_figure("Spectra", fig, global_step=epoch)

                spec_in, _ = [torch.cat(x, dim=0) for x in zip(*list(self.val_loader))]
                spec_in = spec_in.to(self.device)
                z, _ = self.encoder(spec_in)
                if self.verbose:
                    fig = self.get_style_distribution_plot(z.clone().cpu().detach().numpy())
                    self.tb_writer.add_figure("Style Value Distribution", fig, global_step=epoch)

        # save model
        model_dict = {"Encoder": self.encoder,
                      "Decoder": self.decoder,
                      "Style Discriminator": self.discriminator}
        torch.save(model_dict,
                   f'{self.work_dir}/final.pt')
        if best_chk is not None:
            shutil.copy2(best_chk, f'{self.work_dir}/best.pt')

        return metrics

    @classmethod
    def from_data(cls, csv_fn, igpu=0, batch_size=512, lr=0.01, max_epoch=2000,
                  n_coord_num=3, nstyle=2,
                  dropout_rate=0.5, grad_rev_dropout_rate=0.5, grad_rev_noise=0.1, grad_rev_beta=1.1,
                  use_cnn_dis=False, alpha_flat_step=100, alpha_limit=2.0,
                  sch_factor=0.25, sch_patience=300, spec_noise=0.01,
                  lr_ratio_Reconn=2.0, lr_ratio_Mutual=3.0, lr_ratio_Smooth=0.1, 
                  lr_ratio_Supervise=2.0, lr_ratio_Style=0.5, weight_decay=1e-2,
                  train_ratio=0.7, validation_ratio=0.15, test_ratio=0.15, sampling_exponent=0.6,
                  use_flex_spec_target=False, short_circuit_cn=True, optimizer_name="AdamW",
                  decoder_activation='ReLu', ae_form='normal',
                  chem_dict=None, verbose=True, work_dir='.'):
        ae_cls_dict = {"normal": {"encoder": Encoder, "decoder": Decoder},
                       "compact": {"encoder": CompactEncoder, "decoder": CompactDecoder}}
        assert ae_form in ae_cls_dict
        dl_train, dl_val, dl_test = get_dataloaders(
            csv_fn, batch_size, (train_ratio, validation_ratio, test_ratio), sampling_exponent, n_coord_num)
        
        use_cuda = torch.cuda.is_available()
        if use_cuda:
            if verbose:
                print("Use GPU")
            for loader in [dl_train, dl_val]:
                loader.pin_memory = False
        else:
            if verbose:
                print("Use Slow CPU!")

        device = torch.device(f"cuda:{igpu}" if use_cuda else "cpu")

        encoder = ae_cls_dict[ae_form]["encoder"](nclasses=n_coord_num, nstyle=nstyle, dropout_rate=dropout_rate)
        decoder = ae_cls_dict[ae_form]["decoder"](nclasses=n_coord_num, nstyle=nstyle, dropout_rate=dropout_rate, last_layer_activation=decoder_activation)
        if use_cnn_dis:
            discriminator = DiscriminatorCNN(nstyle=nstyle, dropout_rate=grad_rev_dropout_rate, noise=grad_rev_noise)
        else:
            discriminator = DiscriminatorFC(nstyle=nstyle, dropout_rate=grad_rev_dropout_rate, noise=grad_rev_noise)

        for i in [encoder, decoder, discriminator]:
            i.to(device)

        trainer = Trainer(encoder, decoder, discriminator, device, dl_train, dl_val,
                          val_sampling_weights_per_cn=dl_val.dataset.sampling_weights_per_cn,
                          nstyle=nstyle, n_coord_num=n_coord_num, weight_decay=weight_decay,
                          max_epoch=max_epoch, base_lr=lr, use_cnn_dis=use_cnn_dis,
                          grad_rev_beta=grad_rev_beta, alpha_flat_step=alpha_flat_step, alpha_limit=alpha_limit,
                          sch_factor=sch_factor, sch_patience=sch_patience, spec_noise=spec_noise,
                          lr_ratio_Reconn=lr_ratio_Reconn, lr_ratio_Mutual=lr_ratio_Mutual,
                          lr_ratio_Smooth=lr_ratio_Smooth, lr_ratio_Supervise=lr_ratio_Supervise,
                          lr_ratio_Style=lr_ratio_Style, chem_dict=chem_dict, optimizer_name=optimizer_name,
                          use_flex_spec_target=use_flex_spec_target, short_circuit_cn=short_circuit_cn,
                          verbose=verbose, work_dir=work_dir)
        return trainer

    @staticmethod
    def test_models(csv_fn, n_coord_num=3,
                    train_ratio=0.7, validation_ratio=0.15, test_ratio=0.15, sampling_exponent=0.6, work_dir='.',
                    final_model_name='final.pt', best_model_name='best.pt'):
        final_spuncat = torch.load(f'{work_dir}/{final_model_name}', map_location=torch.device('cpu'))
        best_spuncat = torch.load(f'{work_dir}/{best_model_name}', map_location=torch.device('cpu'))

        transform_list = transforms.Compose([ToTensor()])
        dataset_train, dataset_val, dataset_test = [CoordNumSpectraDataset(
            csv_fn, p, (train_ratio, validation_ratio, test_ratio), sampling_exponent, n_coord_num=n_coord_num,
            transform=transform_list)
            for p in ["train", "val", "test"]]
        
        def cluster_grid_plot(decoder):
            decoder.eval()
            for istyle in range(decoder.nstyle):
                nspec_pc = 10
                Idx = np.arange(n_coord_num).repeat(nspec_pc)
                one_hot = np.zeros((n_coord_num * nspec_pc, n_coord_num))
                one_hot[list(range(n_coord_num * nspec_pc)), Idx] = 1

                c = np.linspace(*[-2, 2], nspec_pc).reshape(1, -1)
                c = np.repeat(c, n_coord_num, 0).reshape(-1, 1)
                c2 = np.hstack([np.zeros_like(c)] * istyle + [c] + [np.zeros_like(c)] * (decoder.nstyle - istyle - 1))

                dis_c = torch.tensor(one_hot, dtype=torch.float, requires_grad=False)
                con_c = torch.tensor(c2, dtype=torch.float, requires_grad=False)

                spec_out = decoder(con_c, dis_c).reshape(n_coord_num, nspec_pc, -1).clone().cpu().detach().numpy()
                plt.figure()
                # noinspection PyTypeChecker
                fig, ax_list = plt.subplots(n_coord_num, 1,
                                            sharex=True, sharey=True, figsize=(9, 12))
                colors = sns.color_palette("coolwarm", nspec_pc)
                for i, (sl, ax) in enumerate(zip(spec_out, ax_list.ravel())):
                    for spec, color in zip(sl, colors):
                        ax.plot(spec, lw=1.5, c=color)
                        ax.set_ylabel(f"{i + 4} Folds Coordinated")
                title = f"All Classes and Styles #{istyle}"
                fig.suptitle(title, y=0.91)
                report_dir = os.path.join(work_dir, "reports")
                if not os.path.exists(report_dir):
                    os.makedirs(report_dir)
                plt.savefig(f"{report_dir}/{title}.pdf", dpi=600)
        
        cluster_grid_plot(final_spuncat["Decoder"])

        def plot_style_distributions(encoder, ds, title_base="Style Distribution"):
            encoder.eval()
            spec_in, cn_in = torch.tensor(ds.spec.copy(), dtype=torch.float32), torch.tensor(ds.cn.copy(), dtype=torch.float32)
            z = encoder(spec_in)[0].clone().detach().cpu().numpy()
            nstyle = z.shape[1]
            # noinspection PyTypeChecker
            fig, ax_list = plt.subplots(nstyle, 1, sharex=True, sharey=True, figsize=(9, 12))
            for istyle, ax in zip(range(nstyle), ax_list):
                sns.distplot(z[:, istyle], kde=False, color='blue', bins=np.arange(-3.0, 3.01, 0.2),
                             ax=ax)
                ax.set_xlabel(f"Style #{istyle}")
                ax.set_ylabel("Counts")

            title = f'{title_base}'
            fig.suptitle(title, y=0.91)

            report_dir = os.path.join(work_dir, "reports")
            if not os.path.exists(report_dir):
                os.makedirs(report_dir)
            plt.savefig(f'{report_dir}/{title}.pdf', dpi=300, bbox_inches='tight')

        plot_style_distributions(final_spuncat["Encoder"], dataset_test, 
                                 title_base="Style Distribution on FEFF Test Set")

        def plot_cluster_centers(p, nclasses=n_coord_num, title='final cluster center'):
            p.eval()
            nstyle = p.nstyle
            one_hot = torch.eye(nclasses)
            z = torch.zeros(nclasses, nstyle)
            cluster_specs = p(z, one_hot).cpu().detach().numpy()
            plt.figure()
            sns.set_palette('husl', nclasses)
            for spec in cluster_specs:
                plt.plot(spec)

            plt.title(title)
            report_dir = os.path.join(work_dir, "reports")
            if not os.path.exists(report_dir):
                os.makedirs(report_dir)
            plt.savefig(f'{report_dir}/{title}.pdf', dpi=300)

        plot_cluster_centers(final_spuncat["Decoder"], title='final cluster center')
        plot_cluster_centers(best_spuncat["Decoder"], title='best cluster center')

        def compute_confusion_matrix(encoder, ds, title_base):
            encoder.eval()
            spec_in, cn_in = torch.tensor(ds.spec.copy(), dtype=torch.float32), torch.tensor(ds.cn.copy(), dtype=torch.float32)
            _, y = encoder(spec_in)
            class_probs = y.detach().cpu().numpy()
            class_pred = class_probs.argmax(axis=-1)
            class_true = np.fabs(cn_in.detach().cpu().numpy()).argmax(axis=-1)
            valid_cn_selector = (cn_in.detach().cpu().numpy() >= 0).all(axis=1)
            class_pred = class_pred[valid_cn_selector]
            class_true = class_true[valid_cn_selector]
            cat_accuracy = f1_score(class_true, class_pred, average='weighted')
            cm = confusion_matrix(class_true, class_pred)
            cn_labels = [f'CN{i}' for i in range(4, 4 + n_coord_num)]
            plt.figure()
            sns.set(font_scale=1.4)
            sns.heatmap(cm, xticklabels=cn_labels, yticklabels=cn_labels, cmap='Blues', fmt='d', annot=True, lw=1.0)
            title = f'{title_base} with F1 Score at {cat_accuracy:.2%}'
            plt.title(title)
            plt.xlabel("Prediction")
            plt.ylabel("True Value")
            report_dir = os.path.join(work_dir, "reports")
            if not os.path.exists(report_dir):
                os.makedirs(report_dir)
            plt.savefig(f'{report_dir}/{title_base}.pdf', dpi=300, bbox_inches='tight')
            return cm, cat_accuracy

        compute_confusion_matrix(final_spuncat["Encoder"], dataset_train,
                                 title_base="Confusion Matrix on FEFF Training Set")
        compute_confusion_matrix(final_spuncat["Encoder"], dataset_val,
                                 title_base="Confusion Matrix on FEFF Validation Set")
        compute_confusion_matrix(final_spuncat["Encoder"], dataset_test, 
                                 title_base="Confusion Matrix on FEFF Test Set")
