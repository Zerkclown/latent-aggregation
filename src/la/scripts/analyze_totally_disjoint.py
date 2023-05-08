import json
import logging
import random
from functools import partial

import hydra
import matplotlib.pyplot as plt
import omegaconf
import pytorch_lightning
from pytorch_lightning.utilities.types import STEP_OUTPUT
import torch
import torchmetrics
from datasets import Dataset, concatenate_datasets
from nn_core.common import PROJECT_ROOT
from nn_core.common.utils import seed_index_everything
from omegaconf import DictConfig
from pytorch_lightning import Trainer, seed_everything
from functools import partial

from torch import nn
from torch.nn import functional as F
import torch.nn as nn

import la  # noqa
from la.utils.cka import CKA
from la.utils.class_analysis import Classifier
from la.utils.utils import MyDatasetDict, add_tensor_column, save_dict_to_file
from pytorch_lightning import Trainer

from la.utils.class_analysis import Classifier

pylogger = logging.getLogger(__name__)


def run(cfg: DictConfig) -> str:
    """
    Main entry point for the experiment.
    """
    seed_index_everything(cfg)

    all_cka_results = {
        dataset_name: {model_name: {} for model_name in cfg.model_names} for dataset_name in cfg.dataset_names
    }
    all_class_results = {
        dataset_name: {model_name: {} for model_name in cfg.model_names} for dataset_name in cfg.dataset_names
    }
    all_knn_results = {
        dataset_name: {model_name: {} for model_name in cfg.model_names} for dataset_name in cfg.dataset_names
    }

    for single_cfg in cfg.configurations:
        cka_results, class_results, knn_results = single_configuration_experiment(cfg, single_cfg)

        all_cka_results[single_cfg.dataset_name][single_cfg.model_name] = cka_results

        all_class_results[single_cfg.dataset_name][single_cfg.model_name] = class_results

        all_knn_results[single_cfg.dataset_name][single_cfg.model_name] = knn_results

    save_dict_to_file(path=cfg.cka_results_path, content=all_cka_results)
    save_dict_to_file(path=cfg.class_results_path, content=all_class_results)
    save_dict_to_file(path=cfg.knn_results_path, content=all_knn_results)


def single_configuration_experiment(global_cfg: DictConfig, single_cfg: DictConfig):
    """
    Run a single experiment with the given configurations.

    :param global_cfg: shared configurations for the suite of experiments
    :param single_cfg: configurations for the current experiment
    """
    dataset_name, model_name = (
        single_cfg.dataset_name,
        single_cfg.model_name,
    )

    pylogger.info(f"Running experiment on {dataset_name} embedded with {model_name}.")

    dataset_path = f"{PROJECT_ROOT}/data/{dataset_name}/totally_disjoint/partition-1_{model_name}"

    data: MyDatasetDict = MyDatasetDict.load_from_disk(dataset_dict_path=dataset_path)

    num_tasks = data["metadata"]["num_tasks"]
    num_total_classes = global_cfg.num_total_classes[dataset_name]

    map_labels_to_global(data, num_tasks)

    tensor_columns = ["embedding", "y", "id"]
    set_torch_format(data, num_tasks, modes=["train", "test", "anchors"], tensor_columns=tensor_columns)

    num_anchors = len(data["task_0_anchors"])

    SUBSAMPLE_ANCHORS = global_cfg.num_anchors < num_anchors

    if SUBSAMPLE_ANCHORS:
        pylogger.info(f"Selecting only {global_cfg.num_anchors} anchors out of {num_anchors}")
        num_anchors = global_cfg.num_anchors
        for task in range(num_tasks + 1):
            anchors_subsample = data[f"task_{task}_anchors"].select(range(num_anchors))
            data[f"task_{task}_anchors"] = anchors_subsample

    check_same_anchor_ids(data, num_tasks)

    centering = False
    if centering:
        for task_ind in range(num_tasks + 1):
            for mode in ["train", "test", "anchors"]:
                embedding_mean = data[f"task_{task_ind}_{mode}"]["embedding"].mean(dim=0)
                data[f"task_{task_ind}_{mode}"] = data[f"task_{task_ind}_train"].map(
                    lambda row: {"embedding": row["embedding"] - embedding_mean}
                )

    # map to relative
    for task_ind in range(0, num_tasks + 1):
        task_anchors = data[f"task_{task_ind}_anchors"]["embedding"]
        norm_anchors = F.normalize(task_anchors, p=2, dim=-1)

        for mode in ["train", "test"]:
            task_embeddings = data[f"task_{task_ind}_{mode}"]["embedding"]

            abs_space = F.normalize(task_embeddings, p=2, dim=-1)

            rel_space = abs_space @ norm_anchors.T

            data[f"task_{task_ind}_{mode}"] = add_tensor_column(
                data[f"task_{task_ind}_{mode}"], "relative_embeddings", rel_space
            )

    tensor_columns = tensor_columns + ["relative_embeddings"]
    set_torch_format(data, num_tasks, modes=["train", "test"], tensor_columns=tensor_columns)

    merged_dataset_train = concatenate_datasets([data[f"task_{i}_train"] for i in range(1, num_tasks + 1)])
    merged_dataset_test = concatenate_datasets([data[f"task_{i}_test"] for i in range(1, num_tasks + 1)])

    # sort the datasets by ID to have a consistent order
    original_dataset_train = data[f"task_0_train"].sort("id")
    original_dataset_test = data[f"task_0_test"].sort("id")

    merged_dataset_train = merged_dataset_train.sort("id")
    merged_dataset_test = merged_dataset_test.sort("id")

    # this fails because original_dataset_train has more samples than merged_dataset_train because of the anchors
    # assert torch.all(torch.eq(merged_dataset_train["id"], original_dataset_train["id"]))

    assert torch.all(torch.eq(merged_dataset_test["id"], original_dataset_test["id"]))

    # CKA analysis

    cka = CKA(mode="linear", device="cuda")

    cka_rel_abs = cka(merged_dataset_test["relative_embeddings"], merged_dataset_test["embedding"])

    cka_tot = cka(merged_dataset_test["relative_embeddings"], original_dataset_test["relative_embeddings"])

    cka_results = {
        "cka_rel_abs": cka_rel_abs.detach().item(),
        "cka_tot": cka_tot.detach().item(),
    }

    # KNN classification experiment
    knn_results_original_abs = run_knn_class_experiment(
        num_total_classes, train_dataset=original_dataset_train, test_dataset=original_dataset_test, use_relatives=False
    )

    knn_results_original_rel = run_knn_class_experiment(
        num_total_classes, train_dataset=original_dataset_train, test_dataset=original_dataset_test, use_relatives=True
    )

    knn_results_merged = run_knn_class_experiment(
        num_total_classes, train_dataset=merged_dataset_train, test_dataset=merged_dataset_test, use_relatives=True
    )

    knn_results = {
        "original_abs": knn_results_original_abs,
        "original_rel": knn_results_original_rel,
        "merged": knn_results_merged,
    }

    # Classification analysis

    class_exp = partial(
        run_classification_experiment,
        num_total_classes=num_total_classes,
        classifier_embed_dim=global_cfg.classifier_embed_dim,
    )

    class_results_original_abs = class_exp(
        train_dataset=original_dataset_train,
        test_dataset=original_dataset_test,
        use_relatives=False,
        input_dim=original_dataset_train["embedding"].shape[1],
    )

    class_results_original_rel = class_exp(
        train_dataset=original_dataset_train,
        test_dataset=original_dataset_test,
        use_relatives=True,
        input_dim=num_anchors,
    )

    class_results_merged = class_exp(
        train_dataset=merged_dataset_train,
        test_dataset=merged_dataset_test,
        use_relatives=True,
        input_dim=num_anchors,
    )

    class_results = {
        "original_abs": class_results_original_abs,
        "original_rel": class_results_original_rel,
        "merged": class_results_merged,
    }

    return cka_results, class_results, knn_results


def map_labels_to_global(data, num_tasks):
    for task_ind in range(1, num_tasks + 1):
        global_to_local_map = data["metadata"]["global_to_local_class_mappings"][f"task_{task_ind}"]
        local_to_global_map = {v: int(k) for k, v in global_to_local_map.items()}

        for mode in ["train", "val", "test"]:
            data[f"task_{task_ind}_{mode}"] = data[f"task_{task_ind}_{mode}"].map(
                lambda row: {"y": local_to_global_map[row["y"].item()]},
                desc="Mapping labels back to global.",
            )


def set_torch_format(data, num_tasks, modes, tensor_columns):
    for task_ind in range(0, num_tasks + 1):
        for mode in modes:
            key = f"task_{task_ind}_{mode}"
            if key in data:
                data[key].set_format(type="torch", columns=tensor_columns)


def check_same_anchor_ids(data, num_tasks):
    for task_i in range(num_tasks + 1):
        for task_j in range(task_i, num_tasks + 1):
            assert torch.all(data[f"task_{task_i}_anchors"]["id"] == data[f"task_{task_j}_anchors"]["id"])


def run_classification_experiment(
    num_total_classes: int,
    input_dim: int,
    train_dataset,
    test_dataset,
    use_relatives: bool,
    classifier_embed_dim: int,
):
    """ """
    seed_everything(42)

    dataloader_func = partial(
        torch.utils.data.DataLoader,
        batch_size=128,
        num_workers=8,
    )

    trainer_func = partial(Trainer, gpus=1, max_epochs=100, logger=False, enable_progress_bar=True)

    classifier = Classifier(
        input_dim=input_dim,
        classifier_embed_dim=classifier_embed_dim,
        num_classes=num_total_classes,
    )
    model = Model(
        classifier=classifier,
        use_relatives=use_relatives,
    )
    trainer = trainer_func(callbacks=[pytorch_lightning.callbacks.EarlyStopping(monitor="val_loss", patience=10)])

    split_dataset = train_dataset.train_test_split(test_size=0.1, seed=42)
    train_dataset = split_dataset["train"]
    val_dataset = split_dataset["test"]

    train_dataloader = dataloader_func(train_dataset, shuffle=True)
    val_dataloader = dataloader_func(val_dataset, shuffle=False)
    test_dataloader = dataloader_func(test_dataset, shuffle=False)

    trainer.fit(model, train_dataloader, val_dataloader)

    results = trainer.test(model, test_dataloader)[0]

    results = {
        "total_acc": results["test_acc"],
    }

    return results


def run_knn_class_experiment(
    num_total_classes: int,
    train_dataset,
    test_dataset,
    use_relatives: bool,
):
    seed_everything(42)
    torch.backends.cudnn.deterministic = True

    dataloader_func = partial(
        torch.utils.data.DataLoader,
        batch_size=128,
        num_workers=8,
    )

    trainer_func = partial(Trainer, gpus=1, max_epochs=1, logger=False, enable_progress_bar=True)

    model = KNNClassifier(train_dataset, num_total_classes, use_relatives=use_relatives)
    trainer = trainer_func()

    train_dataloader = dataloader_func(train_dataset, shuffle=True)
    test_dataloader = dataloader_func(test_dataset, shuffle=False)

    trainer.fit(model, train_dataloader)

    results = trainer.test(model, test_dataloader)[0]

    results = {
        "total_acc": results["test_acc"],
    }

    return results


def compute_prototypes(x, y, num_classes):
    # create prototypes
    prototypes = []
    for i in range(num_classes):
        samples_class_i = x[y == i]
        prototype = torch.mean(samples_class_i, dim=0)
        prototypes.append(prototype)

    prototypes = torch.stack(prototypes)

    return prototypes


class KNNClassifier(pytorch_lightning.LightningModule):
    def __init__(self, train_dataset, num_classes, use_relatives):
        super().__init__()
        self.train_dataset = train_dataset
        self.num_classes = num_classes
        self.accuracy = torchmetrics.Accuracy()
        self.embedding_key = "relative_embeddings" if use_relatives else "embedding"

    def on_train_epoch_end(self) -> None:
        prototypes = compute_prototypes(
            self.train_dataset[self.embedding_key], self.train_dataset["y"], num_classes=self.num_classes
        )
        self.register_buffer("prototypes", prototypes)

    def forward(self, x):
        distances = torch.cdist(x, self.prototypes)

        predictions = torch.argmin(distances, dim=1)

        return predictions

    def training_step(self, *args, **kwargs) -> STEP_OUTPUT:
        pass

    def test_step(self, batch, batch_idx):
        assert self.prototypes is not None
        x, y = batch[self.embedding_key], batch["y"]
        y_hat = self(x)

        test_acc = self.accuracy(y_hat, y)
        self.log("test_acc", test_acc, on_step=False, on_epoch=True, prog_bar=True)

    def configure_optimizers(self):
        pass


class Model(pytorch_lightning.LightningModule):
    def __init__(
        self,
        classifier: nn.Module,
        use_relatives: bool,
    ):
        super().__init__()
        self.classifier = classifier

        self.accuracy = torchmetrics.Accuracy()

        self.use_relatives = use_relatives
        self.embedding_key = "relative_embeddings" if self.use_relatives else "embedding"

    def forward(self, x):
        return self.classifier(x)

    def training_step(self, batch, batch_idx):
        x, y = batch[self.embedding_key], batch["y"]
        y_hat = self(x)
        loss = F.cross_entropy(y_hat, y)
        self.log("train_loss", loss, on_epoch=True, prog_bar=True)
        return loss

    def validation_step(self, batch, batch_idx):
        x, y = batch[self.embedding_key], batch["y"]
        y_hat = self(x)
        loss = F.cross_entropy(y_hat, y)
        self.log("val_loss", loss, on_epoch=True, prog_bar=True)

        val_acc = self.accuracy(y_hat, y)
        self.log("val_acc", val_acc, on_step=False, on_epoch=True, prog_bar=True)

        return loss

    def test_step(self, batch, batch_idx):
        x, y = batch[self.embedding_key], batch["y"]
        y_hat = self(x)
        loss = F.cross_entropy(y_hat, y)
        self.log("test_loss", loss, on_epoch=True)

        test_acc = self.accuracy(y_hat, y)
        self.log("test_acc", test_acc, on_step=False, on_epoch=True, prog_bar=True)

        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=1e-3)


@hydra.main(config_path=str(PROJECT_ROOT / "conf"), config_name="analyze_totally_disjoint")
def main(cfg: omegaconf.DictConfig):
    run(cfg)


if __name__ == "__main__":
    main()
