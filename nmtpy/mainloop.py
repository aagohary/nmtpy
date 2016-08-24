from shutil import copy

import numpy as np
import time
import os

class MainLoop(object):
    def __init__(self, model, logger, train_args):
        # model instance
        self.model          = model
        # logger
        self.__log          = logger

        # Counters
        self.uctr           = 0   # update ctr
        self.ectr           = 0   # epoch ctr
        self.vctr           = 0   # validation ctr

        self.early_bad      = 0
        self.early_stop     = False

        # By default save best validation results
        # can be disabled in cross-validation mode
        self.save_best      = True

        self.save_iter      = train_args.save_iter
        self.max_updates    = train_args.max_iteration
        self.max_epochs     = train_args.max_epochs
        self.early_patience = train_args.patience
        self.valid_metric   = train_args.valid_metric
        self.valid_start    = train_args.valid_start
        self.beam_size      = train_args.beam_size
        self.njobs          = train_args.njobs
        self.f_valid        = train_args.valid_freq
        self.f_sample       = train_args.sample_freq
        self.f_verbose      = 10

        self.do_sampling    = self.f_sample > 0
        self.do_beam_search = self.valid_metric != 'px'

        # Number of samples to produce
        self.n_samples      = 5

        # Losses and metrics
        self.epoch_losses         = []
        self.valid_losses         = []
        self.valid_metrics        = []

        # TODO: After validation metric stabilizes more or less
        # do frequent validations using perplexity and call
        # beam-search if PX is relatively better more than 10%
        # self.dynamic_validation = train_args.dynamic_validation

        # If f_valid == 0, do validation at end of epochs
        self.epoch_valid    = (self.f_valid == 0)

    # OK
    def _print(self, msg, footer=False):
        """Pretty prints a message."""
        self.__log.info(msg)
        if footer:
            self.__log.info('-' * len(msg))

    # OK
    def save_best_model(self):
        """Overwrites best on-disk model and saves it as a different file optionally."""
        if self.save_best:
            self._print('Saving the best model')
            self.model.save(self.model.model_path + '.npz')

        # Save each best model as different files
        # Can be useful for ensembling
        if self.save_iter:
            self._print('Saving best model at iteration %d' % self.uctr)
            model_path_uidx = '%s.iter%d.npz' % (self.model.model_path, self.uctr)
            copy(self.model.model_path + '.npz', model_path_uidx)

    # TODO
    def __update_lrate(self):
        """Update learning rate by annealing it."""
        # Change self.model.lrate
        pass
    
    # OK
    def _print_loss(self, loss):
        if self.uctr % self.f_verbose == 0:
            self._print("Epoch: %6d, update: %7d, cost: %10.6f" % (self.ectr,
                                                                  self.uctr,
                                                                  loss))

    # OK
    def _train_epoch(self):
        """Represents a training epoch."""
        start = time.time()
        start_uctr = self.uctr
        self._print('Starting Epoch %d' % self.ectr, True)

        batch_losses = []

        # Iterate over batches
        for data in self.model.train_iterator:
            self.uctr += 1
            self.model.set_dropout(True)

            # Forward/backward and get loss
            loss = self.model.train_batch(*data.values())
            batch_losses.append(loss)

            # verbose
            self._print_loss(loss)

            # Should we stop
            if self.uctr == self.max_updates:
                break

            # Update learning rate if requested
            self.__update_lrate()

            # Do sampling
            self.__do_sampling(data)

            # Do validation
            if not self.epoch_valid and self.uctr % self.f_valid == 0:
                self.__do_validation()

            # Should we stop
            if self.early_stop:
                break

        if self.uctr == self.max_updates:
            self._print("Max iteration %d reached." % self.uctr)
            return

        if self.early_stop:
            self._print("Early stopped.")
            return

        # An epoch is finished
        epoch_time = time.time() - start
        up_ctr = self.uctr - start_uctr
        self.dump_epoch_summary(batch_losses, epoch_time, up_ctr)

        # Do validation
        if self.epoch_valid:
            self.__do_validation()

    # OK
    def dump_epoch_summary(self, losses, epoch_time, up_ctr):
        update_time = epoch_time / float(up_ctr)
        mean_loss = np.array(losses).mean()
        self.epoch_losses.append(mean_loss)
        self._print("--> Epoch %d finished with mean loss %.5f (PPL: %4.5f)" % (self.ectr, mean_loss, np.exp(mean_loss)))
        self._print("--> Epoch took %d minutes, %.3f sec/update" % ((epoch_time / 60.0), update_time))

    # OK
    def __do_sampling(self, data):
        """Generates samples and prints them."""
        if self.do_sampling and self.uctr % self.f_sample == 0:
            samples = self.model.generate_samples(data, self.n_samples)
            if samples is not None:
                for src, truth, sample in samples:
                    if src:
                        self._print("Source: %s" % src)
                    self._print.info("Sample: %s" % sample)
                    self._print.info(" Truth: %s" % truth)

    # OK
    def _is_best(self, loss, metric):
        """Determine whether the loss/metric is the best so far."""
        if len(self.valid_losses) == 0:
            # This is the first validation so the best so far
            return True

        # Compare based on metric
        if metric and metric > np.array([m[1] for m in self.valid_metrics]).max():
            return True

        # Compare based on loss
        if loss < np.array(self.valid_losses).min():
            return True

    def __do_validation(self):
        if self.ectr >= self.valid_start:
            # Disable dropout during validation
            self.model.set_dropout(False)

            self.vctr += 1

            # Compute validation loss
            cur_loss = self.model.val_loss()

            # Compute perplexity
            ppl = np.exp(cur_loss)

            self._print("Validation %2d - loss = %5.5f (PPL: %4.5f)" % (self.vctr, cur_loss, ppl))

            metric = None
            # Are we doing translation?
            if self.do_beam_search:
                metric_str, metric = self.model.run_beam_search(beam_size=self.beam_size,
                                                                n_jobs=self.njobs,
                                                                metric=self.valid_metric,
                                                                mode='beamsearch')

                self._print("Validation %2d - %s" % (self.vctr, metric_str))

            if self._is_best(cur_loss, metric):
                self.save_best_model()
                self.early_bad = 0
            else:
                self.early_bad += 1
                self._print("Early stopping patience: %d validation left" % (self.early_patience - self.early_bad))

            # Store values
            self.valid_losses.append(cur_loss)
            if metric:
                self.valid_metrics.append((metric_str, metric))

            self.early_stop = (self.early_bad == self.early_patience)

            self.dump_val_summary()

    def dump_val_summary(self):
        best_valid_idx = np.argmin(np.array(self.valid_losses)) + 1
        best_vloss = self.valid_losses[best_valid_idx - 1]
        best_px = np.exp(best_vloss)
        self._print('--> Current best loss %5.5f at validation %d (PX: %4.5f)' % (best_vloss,
                                                                                  best_px,
                                                                                  best_valid_idx))
        if len(self.valid_metrics) > 0:
            # At least for BLEU and METEOR, higher is better
            best_metric_idx = np.argmax(np.array([m[1] for m in self.valid_metrics])) + 1
            best_metric = self.valid_metrics[best_metric_idx - 1][0]
            self._print('--> Current best %s: %s at validation %d' % (self.valid_metric,
                                                                      best_metric,
                                                                      best_metric_idx))

    def run(self):
        # We start 1st epoch
        while 1:
            self.ectr += 1
            self._train_epoch()

            # Should we stop?
            if self.ectr == self.max_epochs:
                break
