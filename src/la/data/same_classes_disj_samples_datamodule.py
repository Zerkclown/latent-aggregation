import logging
from functools import cached_property, partial
from pathlib import Path
from typing import List, Mapping, Optional, Union

import pytorch_lightning as pl
from nn_core.nn_types import Split
from omegaconf import DictConfig
from pytorch_lightning.utilities.types import EVAL_DATALOADERS
from torch.utils.data import DataLoader
from torch.utils.data.dataloader import default_collate
from datasets import Dataset, concatenate_datasets
from la.data.datamodule import MyDataModule

from la.prelim_exp.prelim_exp_dataset import MyDataset
from la.utils.utils import MyDatasetDict

pylogger = logging.getLogger(__name__)


class SameClassesDisjSamplesDatamodule(MyDataModule):
    def __init__(
        self,
        datasets: DictConfig,
        num_workers: DictConfig,
        batch_size: DictConfig,
        gpus: Optional[Union[List[int], str, int]],
        data_path: Path,
        only_use_sample_num: int = -1,
        train_on_anchors: bool = False,
    ):
        super().__init__(
            datasets=datasets,
            num_workers=num_workers,
            batch_size=batch_size,
            gpus=gpus,
            data_path=data_path,
            only_use_sample_num=only_use_sample_num,
        )

        # all tasks will have the same anchors
        for task_ind in range(self.num_tasks + 1):
            self.data[f"task_{task_ind}_anchors"] = self.data["anchors"]

        self.train_on_anchors = train_on_anchors
        self.seen_tasks = set()

        pylogger.info("Preprocessing done.")

    def setup(self, stage: Optional[str] = None) -> None:
        # to avoid reprocessing the data
        if self.task_ind in self.seen_tasks:
            return

        self.shuffle_train = True

        map_params = {
            "function": lambda x: {"x": self.transform_func(x["x"])},
            "writer_batch_size": 100,
            "num_proc": 1,
        }

        modes = ["train", "val", "test", "anchors"]

        for mode in modes:
            self.data[f"task_{self.task_ind}_{mode}"] = self.data[f"task_{self.task_ind}_{mode}"].map(
                desc=f"Transforming task {self.task_ind} {mode} samples", **map_params
            )

            self.data[f"task_{self.task_ind}_{mode}"].set_format(type="torch", columns=["x", "y"])
            self.datasets[mode] = self.data[f"task_{self.task_ind}_{mode}"]

        if self.train_on_anchors:
            self.datasets["train"] = concatenate_datasets(self.datasets["train"], self.datasets["anchors"])

        self.seen_tasks.add(self.task_ind)