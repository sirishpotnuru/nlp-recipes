# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

# This script reuses some code from
# https://github.com/huggingface/pytorch-pretrained-BERT/blob/master/examples/run_classifier.py

import os

import numpy as np
import torch.nn as nn
import torch.utils.data
from pytorch_pretrained_bert.modeling import BertForSequenceClassification
from pytorch_pretrained_bert.optimization import BertAdam
from tqdm import tqdm

from utils_nlp.common.pytorch_utils import get_device, move_to_device
from utils_nlp.models.bert.common import Language


class BERTSequenceClassifier:
    """BERT-based sequence classifier"""

    def __init__(self, language=Language.ENGLISH, num_labels=2, cache_dir="."):

        """

        Args:
            language: Language passed to pre-trained BERT model to pick the appropriate model
            num_labels: number of unique labels in train dataset
            cache_dir: cache_dir to load pre-trained BERT model. Defaults to "."
        """
        if num_labels < 2:
            raise ValueError("Number of labels should be at least 2.")

        self.language = language
        self.num_labels = num_labels
        self.cache_dir = cache_dir

        # create classifier
        self.model = BertForSequenceClassification.from_pretrained(
            language.value, cache_dir=cache_dir, num_labels=num_labels
        )

        # define optimizer and model parameters
        param_optimizer = list(self.model.named_parameters())
        no_decay = ["bias", "LayerNorm.bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [
                    p
                    for n, p in param_optimizer
                    if not any(nd in n for nd in no_decay)
                ],
                "weight_decay": 0.01,
            },
            {
                "params": [
                    p
                    for n, p in param_optimizer
                    if any(nd in n for nd in no_decay)
                ]
            },
        ]
        self.optimizer_params = optimizer_grouped_parameters
        self.name_parameters = self.model.named_parameters()
        self.state_dict = self.model.state_dict()

    def create_optimizer(
        self,
        num_train_optimization_steps,
        lr=2e-5,
        fp16_allreduce=False,
        use_distributed=False,
        warmup_proportion=None,
    ):

        """
        Method to create an BERT Optimizer based on the inputs from the user.

        Args:
            num_train_optimization_steps(int): Number of optimization steps.
            lr (float): learning rate of the adam optimizer. defaults to 2e-5.
            warmup_proportion (float, optional): proportion of training to
                perform linear learning rate warmup for. e.g., 0.1 = 10% of
                training. defaults to none.
            fp16_allreduce(bool, optional)L if true, use fp16 compression during allreduce
            use_distributed(bool): Use distributed optimizer from horovod.

        Returns:
            pytorch_pretrained_bert.optimization.BertAdam  : A BertAdam optimizer with user
            specified config.

        """
        if use_distributed:
            import horovod.torch as hvd

            hvd.init()
            if torch.cuda.is_available():
                torch.cuda.set_device(hvd.local_rank())
            else:
                print("No GPU available! Using CPU.")

            lr = lr * hvd.size()

        if warmup_proportion is None:
            optimizer = BertAdam(self.optimizer_params, lr=lr)
        else:
            optimizer = BertAdam(
                self.optimizer_params,
                lr=lr,
                t_total=num_train_optimization_steps,
                warmup=warmup_proportion,
            )

        if use_distributed:
            compression = (
                hvd.Compression.fp16
                if fp16_allreduce
                else hvd.Compression.none
            )
            optimizer = hvd.DistributedOptimizer(
                optimizer,
                named_parameters=self.model.named_parameters(),
                compression=compression,
            )

        return optimizer

    @staticmethod
    def create_data_loader(
        dataset, use_distributed=False, batch_size=32, **kwargs
    ):
        """
        Method to create a data loader for a given Tensor dataset.

        Args:
            dataset(torch.utils.data.Dataset): A Tensor dataset.
            use_distributed(bool): Use distributed sampler for the data loader.
            batch_size(int): Batch size.

        Returns:
            torch.utils.data.DataLoader: A torch data loader to the given dataset.

        """

        if use_distributed:
            import horovod.torch as hvd

            hvd.init()
            train_sampler = torch.utils.data.distributed.DistributedSampler(
                dataset, num_replicas=hvd.size(), rank=hvd.rank()
            )
        else:
            train_sampler = torch.utils.data.RandomSampler(dataset)

        data_loader = torch.utils.data.DataLoader(
            dataset, batch_size=batch_size, sampler=train_sampler, **kwargs
        )

        return data_loader

    def save_model(self):
        """
        Method to save the trained model.
        #ToDo: Works for English Language now. Multiple language support needs to be added.

        """
        # Save the model to the outputs directory for capture
        output_dir = "outputs"
        os.makedirs(output_dir, exist_ok=True)

        # Save a trained model, configuration and tokenizer
        model_to_save = (
            self.model.module if hasattr(self.model, "module") else self.model
        )

        # If we save using the predefined names, we can load using `from_pretrained`
        output_model_file = "outputs/bert-large-uncased"
        output_config_file = "outputs/bert_config.json"

        torch.save(model_to_save.state_dict(), output_model_file)
        model_to_save.config.to_json_file(output_config_file)

    def train(
        self,
        train_loader,
        epoch,
        bert_optimizer=None,
        num_epochs=1,
        num_gpus=0,
        lr=2e-5,
        warmup_proportion=None,
        fp16_allreduce=False,
        use_distributed=False,
        num_train_optimization_steps=10,
    ):
        """
        Method to fine-tune the bert classifier using the given training data

        Args:
            epoch:
            lr:
            warmup_proportion:
            use_distributed:
            fp16_allreduce:
            num_train_optimization_steps:
            train_loader(torch DataLoader): Torch Dataloader created from Torch Dataset
            bert_optimizer(optimizer): optimizer can be BERTAdam for local and Dsitributed if Horovod
            num_epochs(int): the number of epochs to run
            num_gpus(int): the number of gpus

        """

        if bert_optimizer is None:
            self.create_optimizer(
                num_train_optimization_steps=num_train_optimization_steps,
                lr=lr,
                warmup_proportion=warmup_proportion,
                fp16_allreduce=fp16_allreduce,
                use_distributed=use_distributed,
            )

        # define loss function
        device = get_device("cpu" if num_gpus == 0 else "gpu")

        if device:
            self.model.cuda()

        loss_func = nn.CrossEntropyLoss().to(device)

        # train
        self.model.train()  # training mode

        token_type_ids_batch = None

        num_print = 1000
        for batch_idx, data in enumerate(train_loader):

            x_batch = data["token_ids"]
            x_batch = x_batch.cuda()

            y_batch = data["labels"]
            y_batch = y_batch.cuda()

            mask_batch = data["input_mask"]
            mask_batch = mask_batch.cuda()

            if data["token_type_ids"] is not None:
                token_type_ids_batch = data["token_type_ids"]
                token_type_ids_batch = token_type_ids_batch.cuda()

            bert_optimizer.zero_grad()

            y_h = self.model(
                input_ids=x_batch,
                token_type_ids=token_type_ids_batch,
                attention_mask=mask_batch,
                labels=None,
            )

            # not sure of this part
            loss = loss_func(y_h, y_batch).mean()
            loss.backward()

            bert_optimizer.synchronize()
            bert_optimizer.step()

            if batch_idx % num_print == 0:
                print(
                    "epoch:{}/{}; batch:{}; loss:{:.6f}".format(
                        epoch, num_epochs, batch_idx + 1, loss.data
                    )
                )

        del [x_batch, y_batch, mask_batch, token_type_ids_batch]
        torch.cuda.empty_cache()

    def predict(self, test_loader, num_gpus=None, probabilities=False):
        """

        Args:
            test_loader(torch Dataloader): Torch Dataloader created from Torch Dataset
            num_gpus (int, optional): The number of gpus to use.
                                      If None is specified, all available GPUs
                                      will be used. Defaults to None.
            probabilities (bool, optional):
                If True, the predicted probability distribution
                is also returned. Defaults to False.

        Returns:
            1darray, dict(1darray, 1darray, ndarray): Predicted classes and target labels or
                a dictionary with classes, target labels, probabilities) if probabilities is True.
        """
        device = get_device("cpu" if num_gpus == 0 else "gpu")
        self.model = move_to_device(self.model, device, num_gpus)

        # score
        self.model.eval()

        preds = []
        test_labels = []
        for i, data in enumerate(tqdm(test_loader, desc="Iteration")):
            x_batch = data["token_ids"]
            x_batch = x_batch.cuda()

            mask_batch = data["input_mask"]
            mask_batch = mask_batch.cuda()

            y_batch = data["labels"]

            token_type_ids_batch = None
            if data["token_type_ids"] is not None:
                token_type_ids_batch = data["token_type_ids"]
                token_type_ids_batch = token_type_ids_batch.cuda()

            with torch.no_grad():
                p_batch = self.model(
                    input_ids=x_batch,
                    token_type_ids=token_type_ids_batch,
                    attention_mask=mask_batch,
                    labels=None,
                )
            preds.append(p_batch.cpu())
            test_labels.append(y_batch)

        preds = np.concatenate(preds)
        test_labels = np.concatenate(test_labels)

        if probabilities:
            return {
                "Predictions": preds.argmax(axis=1),
                "Target": test_labels,
                "classes probabilities": nn.Softmax(dim=1)(
                    torch.Tensor(preds)
                ).numpy(),
            }
        else:
            return preds.argmax(axis=1)
