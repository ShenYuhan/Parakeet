import itertools
import os
import time

import librosa
import numpy as np
import paddle.fluid.dygraph as dg
from paddle import fluid

import utils
from data import LJSpeech
from waveflow_modules import WaveFlowLoss, WaveFlowModule


class WaveFlow():
    def __init__(self, config, checkpoint_dir, parallel=False, rank=0,
                 nranks=1, tb_logger=None):
        self.config = config
        self.checkpoint_dir = checkpoint_dir
        self.parallel = parallel
        self.rank = rank
        self.nranks = nranks
        self.tb_logger = tb_logger

    def build(self, training=True):
        config = self.config
        dataset = LJSpeech(config, self.nranks, self.rank) 
        self.trainloader = dataset.trainloader
        self.validloader = dataset.validloader

#        if self.rank == 0:
#            for i, (audios, mels) in enumerate(self.validloader()):
#                print("audios {}, mels {}".format(audios.dtype, mels.dtype))
#                print("{}: rank {}, audios {}, mels {}".format(
#                    i, self.rank, audios.shape, mels.shape))
#    
#            for i, (audios, mels) in enumerate(self.trainloader):
#                print("{}: rank {}, audios {}, mels {}".format(
#                    i, self.rank, audios.shape, mels.shape))
#
#        exit()

        waveflow = WaveFlowModule("waveflow", config)
        
        # Dry run once to create and initalize all necessary parameters.
        audio = dg.to_variable(np.random.randn(1, 16000).astype(np.float32))
        mel = dg.to_variable(
            np.random.randn(1, config.mel_bands, 63).astype(np.float32))
        waveflow(audio, mel)

        if training:
            optimizer = fluid.optimizer.AdamOptimizer(
                learning_rate=config.learning_rate)
    
            # Load parameters.
            utils.load_parameters(self.checkpoint_dir, self.rank,
                                  waveflow, optimizer,
                                  iteration=config.iteration,
                                  file_path=config.checkpoint)
            print("Rank {}: checkpoint loaded.".format(self.rank))
    
            # Data parallelism.
            if self.parallel:
                strategy = dg.parallel.prepare_context()
                waveflow = dg.parallel.DataParallel(waveflow, strategy)
    
            self.waveflow = waveflow
            self.optimizer = optimizer
            self.criterion = WaveFlowLoss(config.sigma)

        else:
            # Load parameters.
            utils.load_parameters(self.checkpoint_dir, self.rank, waveflow,
                                  iteration=config.iteration,
                                  file_path=config.checkpoint)
            print("Rank {}: checkpoint loaded.".format(self.rank))

            self.waveflow = waveflow

    def train_step(self, iteration):
        self.waveflow.train()

        start_time = time.time()
        audios, mels = next(self.trainloader)
        load_time = time.time()

        outputs = self.waveflow(audios, mels)
        loss = self.criterion(outputs)

        if self.parallel:
            # loss = loss / num_trainers
            loss = self.waveflow.scale_loss(loss)
            loss.backward()
            self.waveflow.apply_collective_grads()
        else:
            loss.backward()

        current_lr = self.optimizer._learning_rate

        self.optimizer.minimize(loss, parameter_list=self.waveflow.parameters())
        self.waveflow.clear_gradients()

        graph_time = time.time()

        if self.rank == 0:
            loss_val = float(loss.numpy()) * self.nranks
            log = "Rank: {} Step: {:^8d} Loss: {:<8.3f} " \
                  "Time: {:.3f}/{:.3f}".format(
                  self.rank, iteration, loss_val,
                  load_time - start_time, graph_time - load_time)
            print(log)

            tb = self.tb_logger
            tb.add_scalar("Train-Loss-Rank-0", loss_val, iteration)
            tb.add_scalar("Learning-Rate", current_lr, iteration)

    @dg.no_grad
    def valid_step(self, iteration):
        self.waveflow.eval()
        tb = self.tb_logger

        total_loss = []
        sample_audios = []
        start_time = time.time()

        for i, batch in enumerate(self.validloader()):
            audios, mels = batch
            valid_outputs = self.waveflow(audios, mels)
            valid_z, valid_log_s_list = valid_outputs

            # Visualize latent z and scale log_s.
            if self.rank == 0 and i == 0:
                tb.add_histogram("Valid-Latent_z", valid_z.numpy(), iteration)
                for j, valid_log_s in enumerate(valid_log_s_list):
                    hist_name = "Valid-{}th-Flow-Log_s".format(j)
                    tb.add_histogram(hist_name, valid_log_s.numpy(), iteration)

            valid_loss = self.criterion(valid_outputs)
            total_loss.append(float(valid_loss.numpy()))

        total_time = time.time() - start_time
        if self.rank == 0:
            loss_val = np.mean(total_loss)
            log = "Test | Rank: {} AvgLoss: {:<8.3f} Time {:<8.3f}".format(
                self.rank, loss_val, total_time)
            print(log)
            tb.add_scalar("Valid-Avg-Loss", loss_val, iteration)

    @dg.no_grad
    def infer(self, iteration):
        self.waveflow.eval()

        config = self.config
        sample = config.sample

        output = "{}/{}/iter-{}".format(config.output, config.name, iteration)
        os.makedirs(output, exist_ok=True)

        filename = "{}/valid_{}.wav".format(output, sample)
        print("Synthesize sample {}, save as {}".format(sample, filename))

        mels_list = [mels for _, mels, _ in self.validloader()]
        start_time = time.time()
        syn_audio = self.waveflow.synthesize(mels_list[sample])
        syn_time = time.time() - start_time
        print("audio shape {}, synthesis time {}".format(
            syn_audio.shape, syn_time))
        librosa.output.write_wav(filename, syn_audio,
            sr=config.sample_rate)

    def save(self, iteration):
        utils.save_latest_parameters(self.checkpoint_dir, iteration,
                                     self.waveflow, self.optimizer)
        utils.save_latest_checkpoint(self.checkpoint_dir, iteration)
